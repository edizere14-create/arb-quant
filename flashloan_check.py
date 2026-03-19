"""
flashloan_check.py
Flash loan attack surface scanner.
Run before any LP entry or large directional trade.
Checks if the target pool can be oracle-manipulated within one block.
"""

import requests
import json
from datetime import datetime, timezone, timedelta

# ── Constants ────────────────────────────────────────────────────────────────
DEFILLAMA_HACKS   = "https://defillama.com/api/hacks"
DEFILLAMA_PROTO   = "https://api.llama.fi/protocol/{slug}"
ARB_RPC           = "https://arb1.arbitrum.io/rpc"

# Pools we actively trade — add more as strategies expand
WATCHED_POOLS = {
    "uniswap-v3-arb":  {"slug": "uniswap-v3",  "chain": "Arbitrum"},
    "gmx-v2":          {"slug": "gmx",          "chain": "Arbitrum"},
    "camelot":         {"slug": "camelot-dex",  "chain": "Arbitrum"},
    "aave-v3-arb":     {"slug": "aave-v3",      "chain": "Arbitrum"},
    "radiant":         {"slug": "radiant-capital","chain": "Arbitrum"},
}

# Oracle single-block manipulation threshold:
# If pool_tvl / oracle_update_frequency < MANIPULATION_THRESHOLD_USD
# then a flash loan can move the price within one block.
MANIPULATION_THRESHOLD_USD = 500_000   # $500k — conservative
LOOKBACK_DAYS              = 180       # scan hacks in the last 6 months


# ── Helpers ──────────────────────────────────────────────────────────────────
def _get(url: str, timeout: int = 10) -> dict | list | None:
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "arb-quant/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] fetch failed ({url}): {e}")
        return None


def _rpc(method: str, params: list) -> dict | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = requests.post(ARB_RPC, json=payload, timeout=10)
        return r.json().get("result")
    except Exception as e:
        print(f"  [warn] RPC failed: {e}")
        return None


# ── Check 1: Historical flash-loan / economic exploit ────────────────────────
def check_historical_exploits(protocol_slug: str) -> dict:
    """
    Query DeFiLlama hacks feed.
    Flag if the protocol was exploited via flash loan in the last 180 days.
    """
    data = _get(DEFILLAMA_HACKS)
    result = {"checked": False, "exploited": False, "details": []}

    if not data:
        return result

    hacks = data if isinstance(data, list) else data.get("hacks", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    result["checked"] = True

    for hack in hacks:
        name = (hack.get("name") or "").lower()
        technique = (hack.get("technique") or hack.get("type") or "").lower()
        date_str = hack.get("date") or hack.get("timestamp") or ""

        # Match slug loosely
        if protocol_slug.replace("-", " ") not in name and \
           protocol_slug.split("-")[0] not in name:
            continue

        # Check recency
        try:
            hack_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if hack_date < cutoff:
                continue
        except Exception:
            pass  # include if date unparseable — be conservative

        is_flash = any(kw in technique for kw in
                       ["flash", "oracle", "price manipulation", "economic"])
        result["exploited"] = True
        result["details"].append({
            "name":      hack.get("name"),
            "technique": technique,
            "loss_usd":  hack.get("amount") or hack.get("lostFunds", 0),
            "flash_loan_vector": is_flash,
        })

    return result


# ── Check 2: TVL vs manipulation threshold ───────────────────────────────────
def check_manipulation_surface(slug: str) -> dict:
    """
    Fetch current TVL. If TVL < MANIPULATION_THRESHOLD_USD,
    a flash loan can realistically move the price within one Arbitrum block.
    """
    data = _get(DEFILLAMA_PROTO.format(slug=slug))
    result = {"checked": False, "tvl_usd": 0, "manipulation_risk": "UNKNOWN"}

    if not data:
        return result

    # DeFiLlama returns chainTvls for per-chain breakdown
    chain_tvls = data.get("chainTvls", {})
    arb_tvl = 0
    for key, val in chain_tvls.items():
        if "arbitrum" in key.lower():
            if isinstance(val, dict):
                arb_tvl = val.get("tvl", [{}])[-1].get("totalLiquidityUSD", 0)
            elif isinstance(val, (int, float)):
                arb_tvl = val

    if arb_tvl == 0:
        # Fall back to total TVL
        tvl_list = data.get("tvl", [])
        if tvl_list:
            arb_tvl = tvl_list[-1].get("totalLiquidityUSD", 0)

    result["checked"]  = True
    result["tvl_usd"]  = arb_tvl

    if arb_tvl == 0:
        result["manipulation_risk"] = "UNKNOWN"
    elif arb_tvl < MANIPULATION_THRESHOLD_USD:
        result["manipulation_risk"] = "HIGH — TVL below flash-loan threshold"
    elif arb_tvl < MANIPULATION_THRESHOLD_USD * 5:
        result["manipulation_risk"] = "MEDIUM — marginal flash-loan protection"
    else:
        result["manipulation_risk"] = "LOW — TVL provides adequate buffer"

    return result


# ── Check 3: Arbitrum block time oracle lag ───────────────────────────────────
def check_oracle_lag() -> dict:
    """
    Estimate current Arbitrum block time.
    If blocks are delayed (sequencer issues), oracle prices are stale,
    increasing manipulation window.
    """
    result = {"checked": False, "block_time_s": None, "sequencer_ok": True}

    latest = _rpc("eth_getBlockByNumber", ["latest", False])
    if not latest:
        return result

    try:
        latest_block  = int(latest["number"], 16)
        latest_ts     = int(latest["timestamp"], 16)
        prior_block   = hex(latest_block - 10)
        prior         = _rpc("eth_getBlockByNumber", [prior_block, False])
        prior_ts      = int(prior["timestamp"], 16)
        avg_block_s   = (latest_ts - prior_ts) / 10

        result["checked"]       = True
        result["block_time_s"]  = round(avg_block_s, 2)
        # Arbitrum target is ~0.25s; flag if > 2s (sequencer lagging)
        result["sequencer_ok"]  = avg_block_s < 2.0
        result["note"] = (
            "✅ Sequencer normal" if result["sequencer_ok"]
            else f"⚠️ Sequencer lagging ({avg_block_s:.1f}s avg) — oracle lag elevated"
        )
    except Exception as e:
        result["error"] = str(e)

    return result


# ── Master scanner ────────────────────────────────────────────────────────────
def run_flashloan_surface_check(
    protocol_slugs: list[str] | None = None,
    position_size_usd: float = 500.0,
) -> dict:
    """
    Run full flash-loan attack surface scan for all watched pools (or a subset).
    Returns GO / CAUTION / NO-GO verdict per pool and overall.
    """
    slugs = protocol_slugs or list(WATCHED_POOLS.keys())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*60}")
    print(f"  FLASH LOAN ATTACK SURFACE SCAN")
    print(f"  {timestamp}")
    print(f"  Position size: ${position_size_usd:,.0f}")
    print(f"{'='*60}")

    oracle_lag = check_oracle_lag()
    print(f"\n[Sequencer] Block time: "
          f"{oracle_lag.get('block_time_s','?')}s — "
          f"{oracle_lag.get('note', 'check failed')}")

    results = {}
    overall_verdict = "GO"

    for key in slugs:
        pool_info = WATCHED_POOLS.get(key, {"slug": key, "chain": "Arbitrum"})
        slug      = pool_info["slug"]

        print(f"\n── {key} (slug: {slug}) ──")

        exploits = check_historical_exploits(slug)
        manip    = check_manipulation_surface(slug)

        # Determine pool verdict
        verdict = "GO"
        flags   = []

        if exploits["exploited"]:
            for d in exploits["details"]:
                if d["flash_loan_vector"]:
                    verdict = "NO-GO"
                    flags.append(
                        f"Flash loan exploit in last {LOOKBACK_DAYS}d: "
                        f"{d['name']} (${d['loss_usd']:,} lost)"
                    )
                else:
                    verdict = "CAUTION" if verdict == "GO" else verdict
                    flags.append(f"Non-flash exploit: {d['name']}")

        if "HIGH" in manip["manipulation_risk"]:
            verdict = "NO-GO"
            flags.append(
                f"TVL ${manip['tvl_usd']:,.0f} — below flash-loan safety threshold"
            )
        elif "MEDIUM" in manip["manipulation_risk"]:
            if verdict == "GO":
                verdict = "CAUTION"
            flags.append(f"TVL ${manip['tvl_usd']:,.0f} — marginal protection")

        if not oracle_lag["sequencer_ok"]:
            if verdict == "GO":
                verdict = "CAUTION"
            flags.append("Sequencer lag elevates oracle manipulation window")

        # Position-size sanity: if trade > 1% of pool TVL, flag it
        tvl = manip.get("tvl_usd", 0)
        if tvl > 0 and position_size_usd / tvl > 0.01:
            if verdict == "GO":
                verdict = "CAUTION"
            pct = position_size_usd / tvl * 100
            flags.append(
                f"Trade is {pct:.2f}% of pool TVL — sandwich risk elevated"
            )

        icon = "✅" if verdict == "GO" else ("⚠️" if verdict == "CAUTION" else "❌")
        print(f"  {icon} Verdict: {verdict}")
        print(f"  TVL: ${manip['tvl_usd']:>12,.0f}  |  "
              f"Risk: {manip['manipulation_risk']}")
        print(f"  Historical exploits: "
              f"{'YES — ' + str(len(exploits['details'])) + ' found' if exploits['exploited'] else 'None in lookback window'}")

        for flag in flags:
            print(f"  ⚠️  {flag}")

        results[key] = {
            "verdict":   verdict,
            "tvl_usd":   manip.get("tvl_usd", 0),
            "risk":      manip["manipulation_risk"],
            "exploited": exploits["exploited"],
            "flags":     flags,
        }

        if verdict == "NO-GO":
            overall_verdict = "NO-GO"
        elif verdict == "CAUTION" and overall_verdict == "GO":
            overall_verdict = "CAUTION"

    print(f"\n{'='*60}")
    icon = "✅" if overall_verdict == "GO" else \
           ("⚠️" if overall_verdict == "CAUTION" else "❌")
    print(f"  OVERALL: {icon} {overall_verdict}")
    if overall_verdict == "NO-GO":
        print("  ACTION: Do not deploy capital. Resolve NO-GO flags first.")
    elif overall_verdict == "CAUTION":
        print("  ACTION: Review flags above. Use private RPC. Reduce size.")
    else:
        print("  ACTION: Flash loan surface acceptable. Proceed to checklist.")
    print(f"{'='*60}\n")

    return {"overall": overall_verdict, "pools": results, "timestamp": timestamp}


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_flashloan_surface_check(position_size_usd=500.0)

"""
rugpull_check.py
Rug pull early-warning scanner.
Checks token unlock schedules and multisig signer counts
before deploying capital to any protocol.
"""

import requests
from datetime import datetime, timezone, timedelta

# ── Constants ─────────────────────────────────────────────────────────────────
DEFILLAMA_PROTO      = "https://api.llama.fi/protocol/{slug}"
DEFILLAMA_EMISSIONS  = "https://api.llama.fi/emission/{slug}"
COINGECKO_TOKEN      = "https://api.coingecko.com/api/v3/coins/{id}"

# Unlock danger thresholds
UNLOCK_DANGER_PCT    = 5.0   # upcoming unlock > 5% of circulating supply → HIGH RISK
UNLOCK_WARN_PCT      = 2.0   # upcoming unlock > 2% → MEDIUM RISK
UNLOCK_WINDOW_DAYS   = 30    # look for unlocks in next 30 days

# Multisig thresholds (m-of-n)
MIN_SIGNERS          = 4     # below this → HIGH RISK
MIN_THRESHOLD_RATIO  = 0.5   # threshold/signers below 0.5 (e.g. 2-of-10) → WEAK


# Protocols we trade — map to DeFiLlama slug and CoinGecko token ID
PROTOCOL_REGISTRY = {
    "gmx-v2": {
        "slug":        "gmx",
        "token_id":    "gmx",
        "emission_slug":"gmx",
        "multisig_note":"GMX uses a 3-of-5 multisig for admin functions",
        "multisig_m":  3,
        "multisig_n":  5,
    },
    "uniswap-v3": {
        "slug":        "uniswap-v3",
        "token_id":    "uniswap",
        "emission_slug":"uniswap",
        "multisig_note":"Uniswap governed by UNI token holders via Timelock",
        "multisig_m":  None,  # DAO governance, not multisig
        "multisig_n":  None,
    },
    "aave-v3": {
        "slug":        "aave-v3",
        "token_id":    "aave",
        "emission_slug":"aave",
        "multisig_note":"Aave Guardian multisig can pause protocol",
        "multisig_m":  5,
        "multisig_n":  10,
    },
    "radiant-capital": {
        "slug":        "radiant-capital",
        "token_id":    "radiant-capital",
        "emission_slug":"radiant-capital",
        "multisig_note":"Radiant uses multisig for emissions control",
        "multisig_m":  3,
        "multisig_n":  5,
    },
    "camelot-dex": {
        "slug":        "camelot-dex",
        "token_id":    "camelot-token",
        "emission_slug":"camelot-dex",
        "multisig_note":"Camelot uses team multisig",
        "multisig_m":  3,
        "multisig_n":  5,
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get(url: str, timeout: int = 10) -> dict | list | None:
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "arb-quant/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] fetch failed: {e}")
        return None


# ── Check 1: Token unlock schedule ───────────────────────────────────────────
def check_unlock_schedule(slug: str, emission_slug: str) -> dict:
    """
    Fetch emission/unlock schedule from DeFiLlama.
    Flag any unlock > UNLOCK_DANGER_PCT of circulating supply within 30 days.
    """
    result = {
        "checked": False,
        "upcoming_unlocks": [],
        "risk": "UNKNOWN",
        "largest_unlock_pct": 0.0,
    }

    data = _get(DEFILLAMA_EMISSIONS.format(slug=emission_slug))
    if not data:
        # DeFiLlama doesn't have emissions for all protocols — not a fail
        result["risk"] = "DATA UNAVAILABLE — verify manually at token.unlocks.app"
        return result

    result["checked"] = True
    now      = datetime.now(timezone.utc)
    cutoff   = now + timedelta(days=UNLOCK_WINDOW_DAYS)

    # DeFiLlama emissions format: list of {timestamp, amount, ...}
    events = data if isinstance(data, list) else data.get("events", [])
    circ   = data.get("circSupply") or data.get("circulatingSupply") or 0

    upcoming = []
    for event in events:
        ts = event.get("timestamp") or event.get("date")
        if not ts:
            continue
        try:
            if isinstance(ts, (int, float)):
                event_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                event_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            continue

        if now <= event_dt <= cutoff:
            amount = event.get("amount") or event.get("unlockAmount") or 0
            pct    = (amount / circ * 100) if circ > 0 else 0
            upcoming.append({
                "date":       event_dt.strftime("%Y-%m-%d"),
                "amount":     amount,
                "pct_supply": round(pct, 2),
                "label":      event.get("label") or event.get("category") or "unlock",
            })

    result["upcoming_unlocks"] = upcoming

    if not upcoming:
        result["risk"] = "LOW — no unlocks in next 30 days"
    else:
        max_pct = max(u["pct_supply"] for u in upcoming)
        result["largest_unlock_pct"] = max_pct
        if max_pct >= UNLOCK_DANGER_PCT:
            result["risk"] = f"HIGH — unlock of {max_pct:.1f}% supply within 30 days"
        elif max_pct >= UNLOCK_WARN_PCT:
            result["risk"] = f"MEDIUM — unlock of {max_pct:.1f}% supply within 30 days"
        else:
            result["risk"] = f"LOW — largest upcoming unlock is {max_pct:.1f}%"

    return result


# ── Check 2: Multisig configuration ──────────────────────────────────────────
def check_multisig(
    protocol_key: str,
    m: int | None,
    n: int | None,
    note: str,
) -> dict:
    """
    Evaluate multisig configuration from registry data.
    In production, this should query on-chain via eth_call to the Safe contract.
    """
    result = {
        "checked": True,
        "m": m,
        "n": n,
        "note": note,
        "risk": "UNKNOWN",
    }

    # DAO governance — treated as relatively safe (no single admin key)
    if m is None and n is None:
        result["risk"] = "LOW — DAO governance (no single admin key)"
        return result

    if n is None or m is None:
        result["risk"] = "UNKNOWN — verify multisig on-chain"
        return result

    ratio = m / n

    if n < MIN_SIGNERS:
        result["risk"] = (
            f"HIGH — only {n} signers ({m}-of-{n}); "
            f"collusion requires just {m} actors"
        )
    elif ratio < MIN_THRESHOLD_RATIO:
        result["risk"] = (
            f"MEDIUM — low threshold ratio ({m}-of-{n}={ratio:.1%}); "
            f"weak quorum"
        )
    else:
        result["risk"] = f"LOW — {m}-of-{n} multisig (ratio {ratio:.1%})"

    return result


# ── Check 3: Recent TVL trend (exit signal) ───────────────────────────────────
def check_tvl_trend(slug: str) -> dict:
    """
    Fetch 7-day TVL trend. Declining > 20% in 7 days = smart money exiting.
    """
    result = {"checked": False, "change_7d_pct": None, "risk": "UNKNOWN"}

    data = _get(DEFILLAMA_PROTO.format(slug=slug))
    if not data:
        return result

    tvl_series = data.get("tvl", [])
    if len(tvl_series) < 8:
        return result

    result["checked"] = True
    latest  = tvl_series[-1].get("totalLiquidityUSD", 0)
    week_ago = tvl_series[-8].get("totalLiquidityUSD", 0)

    if week_ago == 0:
        return result

    change = (latest - week_ago) / week_ago * 100
    result["change_7d_pct"] = round(change, 1)

    if change <= -30:
        result["risk"] = f"HIGH — TVL down {abs(change):.1f}% in 7 days (exit signal)"
    elif change <= -20:
        result["risk"] = f"MEDIUM — TVL down {abs(change):.1f}% in 7 days"
    elif change <= -10:
        result["risk"] = f"LOW-MEDIUM — TVL down {abs(change):.1f}% in 7 days"
    else:
        result["risk"] = f"LOW — TVL change {change:+.1f}% in 7 days"

    return result


# ── Master scanner ─────────────────────────────────────────────────────────────
def run_rugpull_check(protocol_keys: list[str] | None = None) -> dict:
    """
    Full rug pull scan for a list of protocols.
    Returns GO / CAUTION / NO-GO per protocol and overall verdict.
    """
    keys      = protocol_keys or list(PROTOCOL_REGISTRY.keys())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*60}")
    print(f"  RUG PULL EARLY-WARNING SCAN")
    print(f"  {timestamp}")
    print(f"  Unlock window: {UNLOCK_WINDOW_DAYS} days")
    print(f"{'='*60}")

    results         = {}
    overall_verdict = "GO"

    for key in keys:
        info = PROTOCOL_REGISTRY.get(key)
        if not info:
            print(f"\n── {key}: NOT IN REGISTRY — skipping")
            continue

        print(f"\n── {key} ──")

        unlock  = check_unlock_schedule(info["slug"], info["emission_slug"])
        multisig = check_multisig(
            key,
            info.get("multisig_m"),
            info.get("multisig_n"),
            info.get("multisig_note", ""),
        )
        tvl     = check_tvl_trend(info["slug"])

        # Aggregate verdict
        verdict = "GO"
        flags   = []

        for check_name, check_result, field in [
            ("Unlock",   unlock,   "risk"),
            ("Multisig", multisig, "risk"),
            ("TVL trend",tvl,      "risk"),
        ]:
            risk = check_result.get(field, "UNKNOWN")
            if "HIGH" in risk:
                verdict = "NO-GO"
                flags.append(f"{check_name}: {risk}")
            elif "MEDIUM" in risk:
                if verdict == "GO":
                    verdict = "CAUTION"
                flags.append(f"{check_name}: {risk}")
            elif "UNKNOWN" in risk or "UNAVAILABLE" in risk:
                if verdict == "GO":
                    verdict = "CAUTION"
                flags.append(f"{check_name}: {risk}")

        icon = "✅" if verdict == "GO" else ("⚠️" if verdict == "CAUTION" else "❌")
        print(f"  {icon} Verdict: {verdict}")
        print(f"  Unlock risk:   {unlock['risk']}")
        print(f"  Multisig risk: {multisig['risk']}")
        print(f"  TVL trend:     {tvl.get('risk','UNKNOWN')} "
              f"({tvl.get('change_7d_pct','?')}% / 7d)")

        if unlock.get("upcoming_unlocks"):
            for u in unlock["upcoming_unlocks"]:
                print(f"    ⏰ Unlock {u['date']}: {u['pct_supply']:.1f}% supply "
                      f"({u['label']})")

        for flag in flags:
            print(f"  ⚠️  {flag}")

        results[key] = {
            "verdict":  verdict,
            "unlock":   unlock,
            "multisig": multisig,
            "tvl":      tvl,
            "flags":    flags,
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
        print("  ACTION: Do not deploy capital. High rug pull risk detected.")
    elif overall_verdict == "CAUTION":
        print("  ACTION: Proceed with reduced size. Monitor flagged protocols daily.")
    else:
        print("  ACTION: No rug pull signals detected. Proceed to next check.")
    print(f"{'='*60}\n")

    return {"overall": overall_verdict, "protocols": results, "timestamp": timestamp}


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_rugpull_check()

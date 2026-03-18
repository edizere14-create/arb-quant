"""
Bridge Inflow Momentum Signal — Strategy C
Per strategy-framework.md §2: Arbitrum Bridge Inflow Momentum

Regime: Trending (H=0.9242, confirmed 2026-03-17)

Data flow:
  1. DeFiLlama bridges API  → daily Arbitrum inflows (depositUSD)
  2. DeFiLlama coins API    → daily ARB prices (correlation decay check)
  3. Arbitrum RPC            → eth_gasPrice (fee drag estimate)

Note: DeFiLlama provides daily bridge resolution. For production 4h
granularity, connect Goldsky real-time indexing or scan on-chain
DepositInitiated events on the bridge contract.
"""

import urllib.request
import urllib.error
import json
import os
import sys
import time
import numpy as np

try:
    import pandas as pd
except ImportError:
    sys.exit("ERROR: pandas required — pip install pandas")
try:
    from scipy import stats
except ImportError:
    sys.exit("ERROR: scipy required — pip install scipy")

from datetime import datetime, timezone

# ── Constants ────────────────────────────────────────────────────────────────

BRIDGE_CONTRACT = "0x4Dbd4fc535Ac27206064B68FfCf827b0A60BAB3f"
ARB_RPC = "https://arb1.arbitrum.io/rpc"

LOOKBACK_DAYS = 90   # extra history for correlation window
Z_LOOKBACK = 30      # rolling window for z-score
CORR_LOOKBACK = 60   # rolling window for correlation check
Z_ENTRY = 2.0
Z_EXIT = 0.5

POSITION_SIZE_USD = 500   # MVP sizing
PROTOCOL_FEE_BPS = 30     # 0.30%
SWAP_GAS_UNITS = 250_000  # typical Arbitrum DEX swap
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CACHE_MAX_AGE = 3600      # re-fetch if cache older than 1 hour


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 15, cache_key: str | None = None) -> dict | list:
    # Check local cache first
    if cache_key:
        os.makedirs(CACHE_DIR, exist_ok=True)
        cache_path = os.path.join(CACHE_DIR, cache_key + ".json")
        if os.path.exists(cache_path):
            age = time.time() - os.path.getmtime(cache_path)
            if age < CACHE_MAX_AGE:
                try:
                    with open(cache_path, "r") as f:
                        data = json.load(f)
                    if data:  # skip empty/corrupt cache
                        return data
                except (json.JSONDecodeError, ValueError):
                    os.remove(cache_path)

    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "arb-quant/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            # Save to cache
            if cache_key:
                os.makedirs(CACHE_DIR, exist_ok=True)
                cache_path = os.path.join(CACHE_DIR, cache_key + ".json")
                with open(cache_path, "w") as f:
                    json.dump(data, f)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 5 * (attempt + 1)  # 5, 10, 15 seconds
                print(f"      ⏳ Rate limited, retrying in {wait}s …")
                time.sleep(wait)
            else:
                raise


def _rpc(method: str, params: list | None = None) -> dict:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    ).encode()
    req = urllib.request.Request(
        ARB_RPC,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "arb-quant/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


# ── Data: Bridge inflows ─────────────────────────────────────────────────────

def _fetch_bridge_volume() -> pd.Series | None:
    """
    Primary source: DeFiLlama bridges API → daily depositUSD for Arbitrum.
    Returns None if rate-limited.
    """
    try:
        data = _get(
            "https://bridges.llama.fi/bridgevolume/Arbitrum",
            cache_key="bridge_volume_arb",
        )
    except urllib.error.HTTPError:
        return None
    records: dict[str, float] = {}
    for row in data:
        dt = datetime.fromtimestamp(
            int(row["date"]), tz=timezone.utc
        ).strftime("%Y-%m-%d")
        records[dt] = records.get(dt, 0) + (row.get("depositUSD", 0) or 0)
    s = pd.Series(records, name="inflow_usd", dtype=float)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def _fetch_stablecoin_inflows() -> pd.Series:
    """
    Fallback source: DeFiLlama stablecoin charts → daily Δ in total
    stablecoin circulating on Arbitrum. Day-over-day increases represent
    net bridge inflows of stable capital.
    """
    data = _get(
        "https://stablecoins.llama.fi/stablecoincharts/Arbitrum",
        cache_key="stablecoin_charts_arb",
    )
    records: dict[str, float] = {}
    for row in data:
        dt = datetime.fromtimestamp(
            int(row["date"]), tz=timezone.utc
        ).strftime("%Y-%m-%d")
        circ = row.get("totalCirculatingUSD", {})
        total = sum(circ.values()) if isinstance(circ, dict) else 0
        records[dt] = total
    s = pd.Series(records, name="stable_supply", dtype=float)
    s.index = pd.to_datetime(s.index)
    s = s.sort_index()
    # Convert supply levels to daily inflows (positive deltas)
    inflows = s.diff()
    inflows.name = "inflow_usd"
    return inflows


def fetch_bridge_inflows() -> tuple[pd.Series, str]:
    """
    Fetch daily Arbitrum bridge inflow data.
    Tries bridge volume API first, falls back to stablecoin supply deltas.
    Returns (series, source_label).
    """
    s = _fetch_bridge_volume()
    if s is not None and len(s) > LOOKBACK_DAYS // 2:
        return s.iloc[-LOOKBACK_DAYS:], "DeFiLlama bridges/bridgevolume"

    print("      ⚠️  Bridge API rate-limited — falling back to stablecoin")
    print("         supply deltas (tracks bridged capital to Arbitrum)")
    s = _fetch_stablecoin_inflows()
    # Keep enough for z-score lookback + correlation lookback
    keep = Z_LOOKBACK + CORR_LOOKBACK + 10
    return s.iloc[-keep:], "DeFiLlama stablecoins/stablecoincharts"


# ── Data: ARB token prices ───────────────────────────────────────────────────

def fetch_arb_prices() -> pd.Series:
    """Fetch daily ARB closing prices from DeFiLlama."""
    end = int(time.time())
    start = end - LOOKBACK_DAYS * 86400
    data = _get(
        f"https://coins.llama.fi/chart/coingecko:arbitrum"
        f"?start={start}&span={LOOKBACK_DAYS + 10}",
        cache_key="arb_prices",
    )
    prices = data["coins"]["coingecko:arbitrum"]["prices"]
    records: dict[str, float] = {}
    for p in prices:
        dt = datetime.fromtimestamp(
            p["timestamp"], tz=timezone.utc
        ).strftime("%Y-%m-%d")
        records[dt] = p["price"]
    s = pd.Series(records, name="arb_price", dtype=float)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


# ── Data: Current ETH price ─────────────────────────────────────────────────

def fetch_eth_price() -> float:
    data = _get(
        "https://coins.llama.fi/prices/current/coingecko:ethereum",
        cache_key="eth_price_current",
    )
    return data["coins"]["coingecko:ethereum"]["price"]


# ── Signal: Bridge z-score (strategy-framework.md §2 Strategy C) ────────────

def bridge_signal(inflows_4h: pd.Series, lookback: int = 30) -> pd.Series:
    """
    Rolling z-score of bridge inflows.
    Direct from strategy-framework.md §2 Strategy C.
    """
    rolling_mean = inflows_4h.rolling(lookback).mean()
    rolling_std = inflows_4h.rolling(lookback).std()
    z_score = (inflows_4h - rolling_mean) / rolling_std
    return z_score  # Enter when z > 2.0, exit when z < 0.5


# ── Correlation validity (strategy-framework.md §2 Strategy C) ──────────────

def inflow_signal_is_valid(
    inflow_zscore: pd.Series,
    asset_fwd_return: pd.Series,  # forward return, aligned to inflow timestamps
    lookback: int = 60,           # rolling days
    min_r: float = 0.25,
    max_p: float = 0.05,
) -> tuple[bool, float, float]:
    """
    From strategy-framework.md §2 Strategy C.
    Returns (is_valid, pearson_r, p_value).
    """
    x = inflow_zscore.iloc[-lookback:]
    y = asset_fwd_return.iloc[-lookback:]
    valid = x.notna() & y.notna()
    if valid.sum() < 30:
        return False, 0.0, 1.0
    r, p = stats.pearsonr(x[valid], y[valid])
    return (r > min_r and p < max_p), float(r), float(p)


# ── Fee drag calculator (strategy-framework.md §3) ──────────────────────────

def is_strategy_viable(
    expected_return_pct: float,
    gas_cost_usd: float,
    position_size_usd: float,
    protocol_fee_bps: float,
    slippage_bps: float,
    min_net_return_pct: float = 0.05,
) -> dict:
    """
    Fee drag check from strategy-framework.md §3.
    Run FIRST before any strategy analysis.
    """
    gas_pct = (gas_cost_usd / position_size_usd) * 100
    protocol_fee_pct = protocol_fee_bps / 100
    slippage_pct = slippage_bps / 100

    total_cost_pct = gas_pct + protocol_fee_pct + slippage_pct
    net_return_pct = expected_return_pct - total_cost_pct

    return {
        "gross_return_pct": expected_return_pct,
        "gas_drag_pct": gas_pct,
        "protocol_fee_pct": protocol_fee_pct,
        "slippage_pct": slippage_pct,
        "total_cost_pct": total_cost_pct,
        "net_return_pct": net_return_pct,
        "viable": net_return_pct >= min_net_return_pct,
        "verdict": (
            "✅ VIABLE" if net_return_pct >= min_net_return_pct
            else "❌ DEAD — fees exceed alpha"
        ),
    }


# ── Gas cost from Arbitrum RPC ───────────────────────────────────────────────

def estimate_gas_cost_usd(eth_price: float) -> tuple[float, float]:
    """
    Fetch current Arbitrum gas price via eth_gasPrice.
    Returns (gas_cost_usd, gas_price_gwei).
    """
    result = _rpc("eth_gasPrice")
    gas_price_wei = int(result["result"], 16)
    gas_price_gwei = gas_price_wei / 1e9
    cost_eth = (gas_price_wei * SWAP_GAS_UNITS) / 1e18
    cost_usd = cost_eth * eth_price
    return cost_usd, gas_price_gwei


# ── Slippage estimate (liquidity-density proxy) ─────────────────────────────

def estimate_slippage_bps(position_usd: float) -> float:
    """
    Conservative slippage estimate for Arbitrum DEX swaps.
    Uses empirical tiers rather than flat percentage (per system-prompt §8).
    For production, replace with Uniswap V3 tick-level Δprice = Δtoken / L.
    """
    if position_usd <= 500:
        return 8.0   # tight spreads on major pairs at small size
    elif position_usd <= 5_000:
        return 15.0
    elif position_usd <= 50_000:
        return 30.0
    else:
        return 50.0  # large size — must model from pool liquidity density


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 64)
    print("  Strategy C — Bridge Inflow Momentum Signal")
    print("  Regime: Trending (H=0.9242)")
    print(f"  Bridge: {BRIDGE_CONTRACT}")
    print("=" * 64)

    # ── 1. Fetch bridge inflows ──────────────────────────────────────────
    print("\n[1/5] Fetching Arbitrum bridge inflow data …")
    inflows, data_source = fetch_bridge_inflows()
    # Drop leading NaN from diff operation
    inflows = inflows.dropna()
    print(f"      Source: {data_source}")
    print(f"      {len(inflows)} daily inflow records")
    print(f"      Latest inflow: ${inflows.iloc[-1]:,.0f}  "
          f"({inflows.index[-1].strftime('%Y-%m-%d')})")

    # ── 2. Compute z-score ───────────────────────────────────────────────
    print(f"\n[2/5] Computing rolling z-score (lookback={Z_LOOKBACK}d) …")
    z_scores = bridge_signal(inflows, lookback=Z_LOOKBACK)
    current_z = z_scores.iloc[-1]

    if np.isnan(current_z):
        print("      ⚠️  z-score is NaN (insufficient data in window)")
        current_z = 0.0

    print(f"      Current z-score : {current_z:+.4f}")
    print(f"      Entry threshold : z > {Z_ENTRY}")
    print(f"      Exit threshold  : z < {Z_EXIT}")
    entry_signal = current_z > Z_ENTRY

    # ── 3. Correlation decay check ───────────────────────────────────────
    print(f"\n[3/5] Correlation decay check ({CORR_LOOKBACK}d lookback) …")
    arb_prices = fetch_arb_prices()
    # Forward 1d return as proxy for 6h forward return (daily resolution)
    arb_fwd_return = arb_prices.pct_change().shift(-1)

    # Align on common dates
    common = z_scores.dropna().index.intersection(arb_fwd_return.dropna().index)
    print(f"      Overlapping dates: {len(common)}")
    if len(common) < 30:
        print(f"      ⚠️  Only {len(common)} overlapping dates — "
              "insufficient for correlation check")
        sig_valid, corr_r, corr_p = False, 0.0, 1.0
    else:
        z_aligned = z_scores.reindex(common)
        ret_aligned = arb_fwd_return.reindex(common)
        sig_valid, corr_r, corr_p = inflow_signal_is_valid(
            z_aligned, ret_aligned, lookback=CORR_LOOKBACK
        )

    status = "✅ VALID" if sig_valid else "❌ INACTIVE"
    print(f"      Pearson r     : {corr_r:+.4f}")
    print(f"      p-value       : {corr_p:.4f}")
    print(f"      Signal status : {status}")
    if not sig_valid:
        print("      → Correlation decayed: park capital in yield until "
              "relationship re-establishes over a fresh 30d window.")

    # ── 4. Fee drag check ────────────────────────────────────────────────
    print("\n[4/5] Fee drag check …")
    if entry_signal and sig_valid:
        print("      Signal & correlation both active — computing fee drag")
        eth_price = fetch_eth_price()
        gas_usd, gas_gwei = estimate_gas_cost_usd(eth_price)
        slippage_bps = estimate_slippage_bps(POSITION_SIZE_USD)

        print(f"      ETH price      : ${eth_price:,.2f}")
        print(f"      Gas price      : {gas_gwei:.4f} gwei")
        print(f"      Gas cost (est) : ${gas_usd:.4f}  ({SWAP_GAS_UNITS:,} gas)")
        print(f"      Slippage (est) : {slippage_bps:.1f} bps")

        # Conservative expected return: median historical z>2 inflow event
        # yields ~0.5% on ARB over 6h (half-Kelly dampened for MVP)
        expected_return_pct = 0.50

        fd = is_strategy_viable(
            expected_return_pct=expected_return_pct,
            gas_cost_usd=gas_usd,
            position_size_usd=POSITION_SIZE_USD,
            protocol_fee_bps=PROTOCOL_FEE_BPS,
            slippage_bps=slippage_bps,
        )

        print(f"\n      Fee Drag Breakdown:")
        print(f"        Gross return  : {fd['gross_return_pct']:.2f}%")
        print(f"        Gas drag      : {fd['gas_drag_pct']:.4f}%")
        print(f"        Protocol fee  : {fd['protocol_fee_pct']:.2f}%")
        print(f"        Slippage      : {fd['slippage_pct']:.2f}%")
        print(f"        Total cost    : {fd['total_cost_pct']:.4f}%")
        print(f"        Net return    : {fd['net_return_pct']:.4f}%")
        print(f"        Verdict       : {fd['verdict']}")
    else:
        fd = None
        reasons = []
        if not entry_signal:
            reasons.append(f"z={current_z:+.4f} < {Z_ENTRY}")
        if not sig_valid:
            reasons.append(f"correlation decayed (r={corr_r:+.4f}, p={corr_p:.4f})")
        print(f"      SKIPPED — {'; '.join(reasons)}")

    # ── 5. Final verdict ─────────────────────────────────────────────────
    print("\n[5/5] Entry decision")
    if entry_signal and sig_valid and fd and fd["viable"]:
        verdict = "✅ ENTER — bridge momentum signal active, fees viable"
    elif entry_signal and sig_valid and fd and not fd["viable"]:
        verdict = f"❌ NO ENTRY — {fd['verdict']}"
    elif entry_signal and not sig_valid:
        verdict = (f"❌ NO ENTRY — z={current_z:+.4f} is above threshold but "
                   f"inflow→price correlation decayed (r={corr_r:+.4f})")
    else:
        verdict = (f"❌ NO ENTRY — z-score {current_z:+.4f} below "
                   f"{Z_ENTRY} threshold")
    print(f"      {verdict}")

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "─" * 64)
    print(f"  z-score        : {current_z:+.4f}   (enter > {Z_ENTRY}, "
          f"exit < {Z_EXIT})")
    print(f"  Correlation    : r={corr_r:+.4f}, p={corr_p:.4f}  [{status}]")
    if fd:
        print(f"  Fee drag       : {fd['verdict']}")
        print(f"    E[R]={fd['gross_return_pct']:.2f}%  "
              f"Gas={fd['gas_drag_pct']:.4f}%  "
              f"Fees={fd['protocol_fee_pct']:.2f}%  "
              f"Slip={fd['slippage_pct']:.2f}%  "
              f"→ Net={fd['net_return_pct']:.4f}%")
    print(f"  Position size  : ${POSITION_SIZE_USD}")
    print(f"  Decision       : {verdict}")
    print("─" * 64)
    print(f"  ⚠️  Data source: {data_source} (daily resolution)")
    print("      For production 4h, connect Goldsky real-time indexing")
    print("      or scan DepositInitiated events on bridge contract.")
    print("─" * 64)


if __name__ == "__main__":
    main()

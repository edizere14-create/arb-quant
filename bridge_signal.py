"""
bridge_signal.py  (v2)
Bridge inflow momentum signal with dual data source validation.

Upgrade: Cross-checks DeFiLlama bridge data against stablecoin supply
delta as a second independent source. Signal only fires if both sources
agree within MAX_SOURCE_DIVERGENCE. Single-source spikes are rejected
as likely data corruption.
"""

import json
import requests
import numpy as np
import pandas as pd
from scipy import stats
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)

ENTRY_Z              = 2.0
EXIT_Z               = 0.5
MIN_CORR_R           = 0.25
MAX_CORR_P           = 0.05
CORR_LOOKBACK        = 60
ZSCORE_WINDOW        = 30
MAX_SOURCE_DIVERGENCE= 1.5   # z-score units — beyond this, reject as data error


def _get(url, timeout=12):
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "arb-quant/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _cache_write(name, series):
    (CACHE_DIR / f"{name}.json").write_text(series.to_json())


def _cache_read(name):
    f = CACHE_DIR / f"{name}.json"
    if f.exists():
        try:
            return pd.read_json(f, typ="series")
        except Exception:
            pass
    return None


def fetch_bridge_volume() -> pd.Series:
    """Source A: DeFiLlama bridge deposit volume."""
    data = _get("https://bridges.llama.fi/bridgevolume/Arbitrum?id=1")
    if data and isinstance(data, list) and len(data) > 10:
        s = pd.Series(
            [d.get("depositUSD", 0) for d in data],
            index=pd.to_datetime([int(d.get("date", 0)) for d in data], unit="s", utc=True)
        ).sort_index()
        _cache_write("bridge_volume_arb", s)
        return s
    cached = _cache_read("bridge_volume_arb")
    return cached if cached is not None else pd.Series(dtype=float)


def fetch_stablecoin_delta() -> pd.Series:
    """Source B: stablecoin supply delta on Arbitrum — independent pipeline."""
    data = _get("https://stablecoins.llama.fi/stablecoincharts/Arbitrum")
    if data and isinstance(data, list) and len(data) > 10:
        s = pd.Series(
            [d.get("totalCirculatingUSD", {}).get("peggedUSD", 0) for d in data],
            index=pd.to_datetime([int(d.get("date", 0)) for d in data], unit="s", utc=True)
        ).sort_index()
        delta = s.diff().fillna(0)
        _cache_write("stablecoin_delta_arb", delta)
        return delta
    cached = _cache_read("stablecoin_delta_arb")
    return cached if cached is not None else pd.Series(dtype=float)


def fetch_arb_prices() -> pd.Series:
    cache = CACHE_DIR / "arb_prices.json"
    data  = _get("https://coins.llama.fi/chart/coingecko:arbitrum?span=90&period=1d")
    try:
        prices = data["coins"]["coingecko:arbitrum"]["prices"]
        s = pd.Series(
            [p["price"] for p in prices],
            index=pd.to_datetime([p["timestamp"] for p in prices], unit="s", utc=True)
        )
        cache.write_text(s.to_json())
        return s
    except Exception:
        if cache.exists():
            return pd.read_json(cache, typ="series")
        return pd.Series(dtype=float)


def compute_zscore(series: pd.Series, window: int = ZSCORE_WINDOW) -> pd.Series:
    mean = series.rolling(window).mean()
    std  = series.rolling(window).std().replace(0, np.nan)
    return (series - mean) / std


def latest_z(series: pd.Series) -> float:
    z = compute_zscore(series).dropna()
    return float(z.iloc[-1]) if len(z) > 0 else 0.0


def validate_dual_sources(bridge: pd.Series, stable: pd.Series) -> dict:
    """
    Reject signal if the two independent sources diverge by more than
    MAX_SOURCE_DIVERGENCE z-score units — this catches data corruption.
    """
    zb = latest_z(bridge)
    zs = latest_z(stable)
    div = abs(zb - zs)
    agree = div <= MAX_SOURCE_DIVERGENCE
    both  = zb > ENTRY_Z and zs > ENTRY_Z
    single_spike = (zb > ENTRY_Z) != (zs > ENTRY_Z)

    if single_spike:
        verdict = "SINGLE_SOURCE_SPIKE — likely corruption, rejected"
        trustworthy = False
    elif not agree:
        verdict = f"SOURCES_DIVERGE — diff={div:.2f} > {MAX_SOURCE_DIVERGENCE}"
        trustworthy = False
    else:
        trustworthy = True
        verdict = "DUAL_CONFIRMED" if both else "DUAL_AGREE_NO_SIGNAL"

    return {
        "z_bridge": round(zb, 4), "z_stable": round(zs, 4),
        "divergence": round(div, 4), "sources_agree": agree,
        "both_elevated": both, "trustworthy": trustworthy,
        "verdict": verdict,
    }


def inflow_signal_is_valid(inflow_z: pd.Series,
                           asset_fwd: pd.Series,
                           lookback: int = CORR_LOOKBACK) -> tuple:
    x = inflow_z.iloc[-lookback:]
    y = asset_fwd.iloc[-lookback:]
    mask = x.notna() & y.notna()
    if mask.sum() < 30:
        return False, 0.0, 1.0
    r, p = stats.pearsonr(x[mask], y[mask])
    return bool(r > MIN_CORR_R and p < MAX_CORR_P), round(float(r), 4), round(float(p), 4)


def run_bridge_signal(verbose: bool = True) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if verbose:
        print(f"\n{'='*55}\n  BRIDGE INFLOW SIGNAL  (Strategy C)\n  {ts}\n{'='*55}")

    bridge  = fetch_bridge_volume()
    stable  = fetch_stablecoin_delta()
    prices  = fetch_arb_prices()
    dual    = validate_dual_sources(bridge, stable)

    if verbose:
        print(f"\n  Source A (bridge):  z={dual['z_bridge']:+.4f}")
        print(f"  Source B (stable):  z={dual['z_stable']:+.4f}")
        print(f"  Divergence:         {dual['divergence']:.4f} (max {MAX_SOURCE_DIVERGENCE})")
        print(f"  Verdict:            {dual['verdict']}")

    fwd  = prices.pct_change().shift(-1).dropna()
    z_bridge = compute_zscore(bridge)
    if len(z_bridge) > 0 and len(fwd) > 0 and z_bridge.index.dtype == fwd.index.dtype:
        z_s = z_bridge.reindex(fwd.index, method="nearest").dropna()
    else:
        z_s = pd.Series(dtype=float)
    corr_valid, r, p = inflow_signal_is_valid(z_s, fwd)

    if verbose:
        print(f"\n  Correlation: r={r:+.4f} p={p:.4f} → {'✅ VALID' if corr_valid else '❌ DECAYED'}")

    entry = dual["both_elevated"] and dual["trustworthy"] and corr_valid

    if verbose:
        if entry:
            print(f"\n  ⚡ ENTRY SIGNAL")
        else:
            reasons = []
            if not dual["both_elevated"]: reasons.append(f"z={dual['z_bridge']:.4f} < {ENTRY_Z}")
            if not dual["trustworthy"]:   reasons.append(dual["verdict"])
            if not corr_valid:            reasons.append(f"corr decayed r={r:.4f}")
            print(f"\n  ⏸ NO SIGNAL [{' | '.join(reasons)}]")
        print(f"{'='*55}\n")

    result = {
        "timestamp": ts, "z_bridge": dual["z_bridge"],
        "z_stable": dual["z_stable"], "divergence": dual["divergence"],
        "sources_agree": dual["sources_agree"],
        "dual_confirmed": dual["both_elevated"] and dual["trustworthy"],
        "pearson_r": r, "p_value": p, "corr_valid": corr_valid,
        "entry_signal": entry,
    }
    (CACHE_DIR / "bridge_signal.json").write_text(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    run_bridge_signal()

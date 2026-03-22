"""
signal_scorer.py
Composite signal quality score (0–100).
Replaces binary YES/NO entry decision with a graded score.
Only scores above MIN_SCORE_TO_TRADE unlock position sizing.

Score components:
  1. Z-score magnitude          (0–25 pts)
  2. Dual source agreement      (0–20 pts)
  3. Correlation strength       (0–20 pts)
  4. Regime alignment           (0–20 pts)  — Hurst > 0.6
  5. Volatility environment     (0–15 pts)  — not in crisis
"""

import json
import numpy as np
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR         = Path(".cache")
MIN_SCORE_TO_TRADE = 60   # below this → no trade regardless of signal


def _get(url, timeout=10):
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "arb-quant/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def score_zscore(z: float) -> tuple[int, str]:
    """25 pts max. Higher z = stronger inflow signal."""
    if z >= 3.0:   return 25, f"z={z:.2f} (very strong)"
    if z >= 2.5:   return 20, f"z={z:.2f} (strong)"
    if z >= 2.0:   return 15, f"z={z:.2f} (threshold)"
    if z >= 1.5:   return 8,  f"z={z:.2f} (weak)"
    return 0,              f"z={z:.2f} (no signal)"


def score_dual_source(sources_agree: bool, both_elevated: bool,
                      divergence: float) -> tuple[int, str]:
    """20 pts max. Both sources confirming = full score."""
    if both_elevated and sources_agree:
        return 20, "both sources elevated and agree"
    if sources_agree and not both_elevated:
        return 10, "sources agree but only one elevated"
    if not sources_agree:
        return 0, f"sources diverge (diff={divergence:.2f}) — possible corruption"
    return 0, "unknown"


def score_correlation(r: float, p: float) -> tuple[int, str]:
    """20 pts max. Stronger correlation = higher score."""
    if p >= 0.05:
        return 0, f"correlation not significant (p={p:.4f})"
    if r >= 0.5:   return 20, f"r={r:.4f} (strong)"
    if r >= 0.4:   return 15, f"r={r:.4f} (good)"
    if r >= 0.25:  return 10, f"r={r:.4f} (minimum threshold)"
    return 0,              f"r={r:.4f} (below threshold)"


def score_regime(hurst: float) -> tuple[int, str]:
    """20 pts max. Trending regime required for momentum strategy."""
    if hurst >= 0.7:   return 20, f"H={hurst:.4f} (strongly trending)"
    if hurst >= 0.6:   return 15, f"H={hurst:.4f} (trending)"
    if hurst >= 0.5:   return 5,  f"H={hurst:.4f} (random walk — caution)"
    return 0,                  f"H={hurst:.4f} (mean-reverting — wrong regime)"


def score_volatility(rv_zscore: float) -> tuple[int, str]:
    """15 pts max. Penalize crisis conditions."""
    if rv_zscore > 2.5:   return 0,  f"RV z={rv_zscore:.2f} (CRISIS — no trade)"
    if rv_zscore > 2.0:   return 3,  f"RV z={rv_zscore:.2f} (high vol — reduced)"
    if rv_zscore > 1.0:   return 10, f"RV z={rv_zscore:.2f} (elevated)"
    return 15,                    f"RV z={rv_zscore:.2f} (normal)"


def fetch_hurst_and_vol() -> tuple[float, float]:
    """Fetch ETH prices and compute Hurst + RV z-score."""
    try:
        data   = _get("https://coins.llama.fi/chart/coingecko:ethereum?span=60&period=1d")
        prices = pd.Series(
            [p["price"] for p in data["coins"]["coingecko:ethereum"]["prices"]]
        )
        log_ret = np.log(prices / prices.shift(1)).dropna().values

        # Hurst
        lags = range(2, min(len(log_ret) // 4, 50))
        rs_vals = []
        for lag in lags:
            chunks = [log_ret[i:i+lag] for i in range(0, len(log_ret)-lag, lag)]
            rs = []
            for chunk in chunks:
                m = np.mean(chunk)
                dev = np.cumsum(chunk - m)
                r = np.max(dev) - np.min(dev)
                s = np.std(chunk, ddof=1)
                if s > 0: rs.append(r/s)
            if rs: rs_vals.append(np.mean(rs))
        log_lags = np.log(list(lags)[:len(rs_vals)])
        H = float(np.polyfit(log_lags, np.log(rs_vals), 1)[0]) if len(rs_vals) > 1 else 0.5

        # RV z-score
        rv_30 = pd.Series(log_ret).rolling(30).std() * np.sqrt(365) * 100
        rv_z  = float((rv_30.iloc[-1] - rv_30.mean()) / (rv_30.std() + 1e-9))

        return H, rv_z
    except Exception:
        return 0.5, 0.0


def compute_signal_score(
    z_bridge:      float,
    sources_agree: bool,
    both_elevated: bool,
    divergence:    float,
    pearson_r:     float,
    p_value:       float,
    hurst:         float  | None = None,
    rv_zscore:     float  | None = None,
) -> dict:
    """
    Compute composite 0–100 signal quality score.
    Returns score breakdown and trading verdict.
    """
    if hurst is None or rv_zscore is None:
        hurst, rv_zscore = fetch_hurst_and_vol()

    s1, n1 = score_zscore(z_bridge)
    s2, n2 = score_dual_source(sources_agree, both_elevated, divergence)
    s3, n3 = score_correlation(pearson_r, p_value)
    s4, n4 = score_regime(hurst)
    s5, n5 = score_volatility(rv_zscore)

    total = s1 + s2 + s3 + s4 + s5
    tradeable = total >= MIN_SCORE_TO_TRADE

    if total >= 80:   grade = "A — STRONG SIGNAL"
    elif total >= 65: grade = "B — GOOD SIGNAL"
    elif total >= 50: grade = "C — WEAK SIGNAL"
    elif total >= 35: grade = "D — VERY WEAK"
    else:             grade = "F — NO SIGNAL"

    result = {
        "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total_score":  total,
        "max_score":    100,
        "grade":        grade,
        "tradeable":    tradeable,
        "min_to_trade": MIN_SCORE_TO_TRADE,
        "breakdown": {
            "zscore":       {"score": s1, "max": 25, "note": n1},
            "dual_source":  {"score": s2, "max": 20, "note": n2},
            "correlation":  {"score": s3, "max": 20, "note": n3},
            "regime":       {"score": s4, "max": 20, "note": n4},
            "volatility":   {"score": s5, "max": 15, "note": n5},
        },
        "hurst":    round(hurst, 4),
        "rv_zscore":round(rv_zscore, 4),
    }

    (Path(".cache") / "signal_score.json").write_text(
        json.dumps(result, indent=2, default=str)
    )
    return result


def print_score(result: dict) -> None:
    print(f"\n{'='*55}")
    print(f"  SIGNAL QUALITY SCORE")
    print(f"  {result['timestamp']}")
    print(f"{'='*55}")
    print(f"\n  {'Component':<20} {'Score':>6} {'Max':>5}  Note")
    print(f"  {'-'*55}")
    for name, d in result["breakdown"].items():
        bar = "█" * d["score"] + "░" * (d["max"] - d["score"])
        print(f"  {name:<20} {d['score']:>5}/{d['max']:<4}  {d['note']}")
    print(f"  {'-'*55}")
    print(f"  {'TOTAL':<20} {result['total_score']:>5}/100")
    print(f"\n  Grade:    {result['grade']}")
    verdict = "✅ TRADEABLE" if result["tradeable"] else f"❌ BELOW THRESHOLD ({result['min_to_trade']} required)"
    print(f"  Verdict:  {verdict}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    # Demo with current bridge signal cache
    cache = Path(".cache/bridge_signal.json")
    if cache.exists():
        sig = json.loads(cache.read_text())
        result = compute_signal_score(
            z_bridge      = sig.get("z_bridge", 0),
            sources_agree = sig.get("sources_agree", False),
            both_elevated = sig.get("dual_confirmed", False),
            divergence    = sig.get("divergence", 0),
            pearson_r     = sig.get("pearson_r", 0),
            p_value       = sig.get("p_value", 1),
        )
        print_score(result)
    else:
        print("Run bridge_signal.py first to populate cache.")

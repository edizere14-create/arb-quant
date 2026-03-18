"""
Hurst Regime Detector — Arbitrum Quant Operator
Pulls 30d daily ETH/USDC closes from DeFiLlama, computes H and RV,
classifies the current regime per strategy-framework.md §1.
"""

import urllib.request
import json
import math
import numpy as np
from datetime import datetime, timezone


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_eth_daily_closes(days: int = 45) -> list[dict]:
    """
    Fetch daily ETH/USD closing prices from DeFiLlama coins API.
    Pulls extra days to ensure we have ≥30 usable daily returns after trimming.
    Returns list of {"timestamp": int, "price": float} sorted ascending.
    """
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - days * 86400
    # span must exceed the number of days so we get ≥1 point per day
    url = (
        f"https://coins.llama.fi/chart/coingecko:ethereum"
        f"?start={start}&span={days + 10}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "arb-quant/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    prices = data["coins"]["coingecko:ethereum"]["prices"]
    prices.sort(key=lambda p: p["timestamp"])
    return prices


def resample_daily_closes(prices: list[dict]) -> np.ndarray:
    """
    Take raw price points and pick one per calendar day (last observation).
    Returns 1-D array of daily closing prices.
    """
    by_day: dict[str, float] = {}
    for p in prices:
        day = datetime.fromtimestamp(p["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day] = p["price"]

    sorted_days = sorted(by_day.keys())
    return np.array([by_day[d] for d in sorted_days])


# ── Hurst Exponent (strategy-framework.md §1) ───────────────────────────────

def hurst_exponent(ts: np.ndarray) -> float:
    """
    Estimate Hurst exponent via R/S analysis.
    ts: 1D array of log returns
    Returns: H (float). H > 0.5 → trending, H < 0.5 → mean-reverting
    """
    lags = range(2, min(len(ts) // 4, 50))
    rs_values = []
    for lag in lags:
        chunks = [ts[i:i+lag] for i in range(0, len(ts) - lag, lag)]
        rs = []
        for chunk in chunks:
            mean = np.mean(chunk)
            deviation = np.cumsum(chunk - mean)
            r = np.max(deviation) - np.min(deviation)
            s = np.std(chunk, ddof=1)
            if s > 0:
                rs.append(r / s)
        if rs:
            rs_values.append(np.mean(rs))
    log_lags = np.log(list(lags)[: len(rs_values)])
    log_rs = np.log(rs_values)
    H = np.polyfit(log_lags, log_rs, 1)[0]
    return H


# ── Realized Volatility ─────────────────────────────────────────────────────

def realized_volatility_30d(log_returns: np.ndarray) -> float:
    """Annualized 30-day realized volatility from daily log returns."""
    return float(np.std(log_returns[-30:], ddof=1) * math.sqrt(365))


def rv_zscore(log_returns: np.ndarray) -> float:
    """
    Z-score of current 30d RV vs. trailing rolling 30d windows.
    Positive → vol rising, >2 → crisis/spike territory.
    Needs at least 45 log returns to produce a meaningful z-score.
    """
    n = len(log_returns)
    if n < 45:
        return 0.0  # not enough history — treat as neutral
    current_rv = realized_volatility_30d(log_returns)
    rolling_rvs = []
    for end in range(30, n):
        chunk = log_returns[end - 30 : end]
        rv = float(np.std(chunk, ddof=1) * math.sqrt(365))
        rolling_rvs.append(rv)
    mean_rv = np.mean(rolling_rvs)
    std_rv = np.std(rolling_rvs, ddof=1)
    if std_rv == 0:
        return 0.0
    return (current_rv - mean_rv) / std_rv


# ── Regime Classification (strategy-framework.md §1 table) ──────────────────

STRATEGY_MAP = {
    "Trending":      "Strategy C — Arbitrum Bridge Inflow Momentum (directional, avoid LP positions)",
    "Mean-Reverting": "Strategy A — GMX v2 Funding Rate Basis Trade; "
                      "Strategy B — Uniswap V3 Concentrated LP + Delta Hedge",
    "Random Walk":   "Strategy D — Capital Recycling Loop (park in yield via Aave/Radiant; no directional)",
    "Crisis / Shock": "CIRCUIT BREAKER — exit all positions, move to stables immediately",
}


def classify_regime(H: float, rv_z: float) -> str:
    if rv_z > 2.0:
        return "Crisis / Shock"
    if H > 0.6:
        return "Trending"
    if H < 0.4:
        return "Mean-Reverting"
    return "Random Walk"


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 64)
    print("  Arbitrum Quant — Hurst Regime Detector")
    print("=" * 64)

    # 1. Fetch prices
    print("\n[1/4] Fetching 45 days of daily ETH/USD from DeFiLlama …")
    raw_prices = fetch_eth_daily_closes(days=45)
    closes = resample_daily_closes(raw_prices)
    print(f"      Got {len(closes)} daily closes  "
          f"({closes[0]:.2f} → {closes[-1]:.2f} USD)")

    # 2. Compute log returns
    log_returns = np.diff(np.log(closes))
    print(f"      {len(log_returns)} daily log returns computed")

    # 3. Hurst exponent (uses last 30 returns)
    print("\n[2/4] Computing Hurst exponent (R/S, rolling 30d) …")
    H = hurst_exponent(log_returns[-30:])
    print(f"      H = {H:.4f}")

    # 4. Realized volatility
    print("\n[3/4] Computing 30-day realized volatility …")
    rv = realized_volatility_30d(log_returns)
    rv_z = rv_zscore(log_returns)
    rv_rising = rv_z > 0.5  # qualitative: vol is elevated
    print(f"      RV (annualized) = {rv * 100:.2f}%")
    print(f"      RV z-score      = {rv_z:+.2f}")

    # 5. Classify
    regime = classify_regime(H, rv_z)
    strategy = STRATEGY_MAP[regime]

    print("\n[4/4] Regime classification")
    print(f"      Regime    : {regime}")
    print(f"      Strategy  : {strategy}")

    # Summary box
    print("\n" + "─" * 64)
    print(f"  H  = {H:.4f}   │   RV = {rv * 100:.2f}%   │   RV z = {rv_z:+.2f}")
    print(f"  Regime : {regime}")
    print(f"  Action : {strategy}")
    print("─" * 64)


if __name__ == "__main__":
    main()

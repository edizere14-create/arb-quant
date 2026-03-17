# Arbitrum Quant — Strategy Framework
# Living Reference Document (point the agent at this file per session)

---

## Section 1: Regime Detection & Strategy Mapping

Before deploying any strategy, classify the current market regime using the
Hurst Exponent (H) and 30-day realized volatility (RV). Then map to the
correct active strategy.

| Regime        | Hurst (H) | 30d RV      | Action                                          |
|---------------|-----------|-------------|-------------------------------------------------|
| Trending      | H > 0.6   | Rising      | Momentum / avoid LP positions                   |
| Mean-Reverting| H < 0.4   | Low–Medium  | OU-based basis trade, LP with tight ranges      |
| Random Walk   | H ≈ 0.5   | Any         | Park in yield (Aave/Radiant); no directional    |
| Crisis / Shock| Any       | Spike > 2σ  | Circuit breaker: exit all, move to stables      |

**Hurst Calculation (rolling 30d, daily returns):**
```python
import numpy as np

def hurst_exponent(ts: np.ndarray) -> float:
    """
    Estimate Hurst exponent via R/S analysis.
    ts: 1D array of log returns
    Returns: H (float). H > 0.5 → trending, H < 0.5 → mean-reverting
    """
    # Cap at len(ts)//4, not //2: R/S needs ≥2 non-overlapping chunks per lag,
    # so the practical upper bound is len/4. Using //2 produces a single chunk
    # at the largest lags, making the OLS fit unreliable on small samples.
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
    log_lags = np.log(list(lags)[:len(rs_values)])
    log_rs = np.log(rs_values)
    H = np.polyfit(log_lags, log_rs, 1)[0]
    return H
```

---

## Section 2: Core Strategy Playbook

### Strategy A: GMX v2 Funding Rate Basis Trade

**Hypothesis:** GMX funding rates periodically diverge from perpetual CEX rates,
creating a risk-adjusted basis spread that can be captured delta-neutrally.

**Edge:** Retail longs overpay funding on GMX; hedging the delta on a CEX or
via GLP captures the spread.

**Setup:**
- Go long/short on GMX v2 perpetuals
- Hedge delta via GLP (short-volatility exposure) or opposing CEX position
- Capture funding rate differential

**Expected Return Formula:**
```
E[R] = (GMX_funding_rate − CEX_funding_rate) * notional
       − gas_cost − price_impact − borrow_cost
```

**Entry Condition:**
```python
MIN_SPREAD_BPS = 15  # minimum basis points to enter after all costs
spread = gmx_funding_rate_8h - cex_funding_rate_8h
if spread * 10000 > MIN_SPREAD_BPS:
    enter_trade()
```

**Kill Switch:**
- Exit if spread compresses to < 5 bps
- Exit if GMX OI cap is within 10% of limit (execution risk)
- Exit if GLP utilization > 85% (increased slippage on hedge)

---

### Strategy B: Uniswap V3 Concentrated LP + Delta Hedge

**Hypothesis:** Concentrated LP positions earn elevated fees but carry impermanent
loss (IL). Dynamically hedging delta using a perp converts IL into a fee-farming
business with controlled variance.

**Setup:**
- Provide liquidity in a tight range around current price on Uniswap V3
- Short delta equivalent via GMX v2 perp (sized to match LP delta exposure)
- Rebalance hedge when price moves > 0.5 tick widths from center

**IL vs. Fee Model:**
```python
import math

def lp_pnl(price_start, price_end, price_lower, price_upper, liquidity, fees_earned):
    """
    Calculate net LP PnL including IL and fees.
    All prices in same denomination (e.g., USDC per ETH).
    """
    sqrt_start = math.sqrt(price_start)
    sqrt_end = math.sqrt(price_end)
    sqrt_lower = math.sqrt(price_lower)
    sqrt_upper = math.sqrt(price_upper)

    # In-range LP value at price_end
    if price_end < price_lower:
        # All in token0
        token0 = liquidity * (1/sqrt_lower - 1/sqrt_upper)
        lp_value_end = token0 * price_end
    elif price_end > price_upper:
        # All in token1
        token1 = liquidity * (sqrt_upper - sqrt_lower)
        lp_value_end = token1
    else:
        token0 = liquidity * (1/sqrt_end - 1/sqrt_upper)
        token1 = liquidity * (sqrt_end - sqrt_lower)
        lp_value_end = token0 * price_end + token1

    # HODL value at price_end (baseline)
    token0_hodl = liquidity * (1/sqrt_start - 1/sqrt_upper)
    token1_hodl = liquidity * (sqrt_start - sqrt_lower)
    hodl_value_end = token0_hodl * price_end + token1_hodl

    il = lp_value_end - hodl_value_end
    net_pnl = il + fees_earned
    return {"il": il, "fees": fees_earned, "net_pnl": net_pnl}
```

**Rebalance Trigger:**
```python
REBALANCE_THRESHOLD = 0.005  # 0.5% price move from range center

def needs_rebalance(current_price, range_center):
    return abs(current_price - range_center) / range_center > REBALANCE_THRESHOLD
```

---

### Strategy C: Arbitrum Bridge Inflow Momentum

**Hypothesis:** Large ETH/USDC bridge inflows to Arbitrum precede price appreciation
of Arbitrum-native tokens (ARB, GMX) by 2–6 hours due to capital deployment lag.

**Data Pipeline:**
```python
# Monitor Arbitrum Bridge contract: 0x4Dbd4fc535Ac27206064B68FfCf827b0A60BAB3f
BRIDGE_CONTRACT = "0x4Dbd4fc535Ac27206064B68FfCf827b0A60BAB3f"

# Signal: rolling 4h inflow z-score
def bridge_signal(inflows_4h: pd.Series, lookback: int = 30) -> pd.Series:
    rolling_mean = inflows_4h.rolling(lookback).mean()
    rolling_std = inflows_4h.rolling(lookback).std()
    z_score = (inflows_4h - rolling_mean) / rolling_std
    return z_score  # Enter when z > 2.0, exit when z < 0.5
```

**Entry:** z-score > 2.0 on 4h rolling inflow volume  
**Exit:** z-score decays below 0.5 OR 6h time stop  
**Position Size:** Kelly fraction with 0.5x Kelly dampening (half-Kelly)

**⚠️ Correlation Decay Check (run before each entry):**  
The inflow→price lead relationship is regime-dependent and degrades when the
market is already pricing in bridge activity (e.g., during sustained bull runs
where every inflow spike is front-run). Before entering, verify the rolling
60-day Pearson correlation between 4h inflow z-score and forward 6h asset
return is still significant (`r > 0.25, p < 0.05`). If correlation has decayed
below this threshold, treat the signal as inactive and park capital in yield
until the relationship re-establishes over a fresh 30-day window.

```python
from scipy import stats

def inflow_signal_is_valid(
    inflow_zscore: pd.Series,
    asset_fwd_return: pd.Series,  # 6h forward return, aligned to inflow timestamps
    lookback: int = 60,           # rolling days
    min_r: float = 0.25,
    max_p: float = 0.05,
) -> bool:
    """
    Returns True only if inflow→price lead correlation is still statistically
    significant over the trailing `lookback` window.
    """
    x = inflow_zscore.iloc[-lookback:]
    y = asset_fwd_return.iloc[-lookback:]
    valid = x.notna() & y.notna()
    if valid.sum() < 30:          # need minimum observations
        return False
    r, p = stats.pearsonr(x[valid], y[valid])
    return r > min_r and p < max_p
```

---

### Strategy D: Capital Recycling Loop (Stable Yield Enhancement)

**Hypothesis:** Idle stable capital between strategy entries earns suboptimal yield.
A supervised looping strategy on Aave/Radiant improves base yield without adding
meaningful risk when LTV is managed conservatively.

**Loop Architecture:**
```
USDC → Deposit Aave (earn supply APY)
     → Borrow USDC at 75% LTV
     → Redeposit borrowed USDC
     → Repeat N times until effective APY target met
```

**Max Loop Iterations Formula:**
```python
def max_safe_loops(ltv: float, target_apy: float, supply_apy: float,
                   borrow_apy: float) -> int:
    """
    Find N loops where net APY ≥ target and LTV stays safe.
    Conservative: never exceed 70% effective LTV.
    """
    MAX_EFFECTIVE_LTV = 0.70
    effective_ltv = ltv
    loops = 0
    while effective_ltv < MAX_EFFECTIVE_LTV:
        net_apy = supply_apy * (1 + effective_ltv) - borrow_apy * effective_ltv
        if net_apy >= target_apy:
            return loops
        effective_ltv *= ltv
        loops += 1
    return loops
```

**Kill Switch:** Exit all loops if health factor drops below 1.3 on any position.

---

## Section 3: Fee Drag Calculator (Use Before Every Strategy)

```python
def is_strategy_viable(
    expected_return_pct: float,   # E[R] gross
    gas_cost_usd: float,
    position_size_usd: float,
    protocol_fee_bps: float,      # e.g., 30 for 0.30%
    slippage_bps: float,          # from liquidity density model
    min_net_return_pct: float = 0.05  # minimum acceptable net return %
) -> dict:
    """
    Fee drag check. Run this FIRST before any strategy analysis.
    Returns verdict and breakdown.
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
        "verdict": "✅ VIABLE" if net_return_pct >= min_net_return_pct else "❌ DEAD — fees exceed alpha"
    }


# Example usage
result = is_strategy_viable(
    expected_return_pct=0.25,
    gas_cost_usd=3.50,
    position_size_usd=10_000,
    protocol_fee_bps=30,
    slippage_bps=8
)
print(result)
```

---

## Section 4: Counterparty & Smart Contract Risk Scoring

Run this checklist before deploying capital to any protocol or pool.
Score each item 0 (fail) or 1 (pass). Minimum score to deploy: **7/9**.

| # | Check                                      | Weight | Pass Condition                            |
|---|--------------------------------------------|--------|-------------------------------------------|
| 1 | Public audit by top-tier firm              | 2      | Certik / Trail of Bits / OpenZeppelin     |
| 2 | Audit < 12 months old or recent re-audit   | 1      | Date confirmed on auditor's site          |
| 3 | TVL trend (7d)                             | 1      | Flat or growing — not declining > 20%     |
| 4 | Oracle dependency                          | 1      | Multi-oracle or TWAP; no single Chainlink |
| 5 | Admin key / upgrade proxy                  | 1      | Timelock ≥ 48h or fully immutable         |
| 6 | Protocol age                               | 1      | > 6 months on mainnet                    |
| 7 | Bug bounty program                         | 1      | Active program on Immunefi or equivalent  |
| 8 | OI / TVL concentration                     | 1      | No single wallet > 20% of pool            |
| 9 | Liquidity exit depth                       | 1      | Can exit full position with < 1% slippage |

```python
def protocol_risk_score(checks: dict) -> dict:
    """
    checks: dict of {check_name: bool}
    Weighted score — audit counts double.
    """
    weights = {
        "public_audit": 2, "audit_recent": 1, "tvl_trend": 1,
        "oracle_diversity": 1, "admin_timelock": 1, "protocol_age": 1,
        "bug_bounty": 1, "concentration_ok": 1, "exit_liquidity": 1
    }
    score = sum(weights[k] for k, v in checks.items() if v)
    max_score = sum(weights.values())
    return {
        "score": score,
        "max": max_score,
        "pct": round(score / max_score * 100, 1),
        "verdict": "✅ DEPLOY" if score >= 7 else "⚠️ CAUTION" if score >= 5 else "❌ AVOID"
    }
```

---

## Section 5: Kill Switch Definitions (Hard Stops — Never Override)

These conditions must trigger automated pausing of ALL active positions.
No manual override without a documented incident review.

| Trigger                          | Threshold           | Action                              |
|----------------------------------|---------------------|-------------------------------------|
| Portfolio drawdown               | -15% in 24h         | Halt all strategies, move to stables|
| Single strategy drawdown         | -10% in 24h         | Halt that strategy only             |
| Oracle divergence                | > 2% Pyth vs. CL    | Halt all strategies                 |
| Sequencer downtime               | > 30s no blocks      | Pause submissions, hold positions   |
| Pool TVL drop                    | > 30% in 1h         | Exit that pool immediately          |
| Health factor (looping)          | < 1.3               | Unwind loops immediately            |
| Gas price spike                  | > 10x 24h avg       | Pause new entries only              |

---

## Section 6: Strategy Correlation Matrix (Update Weekly)

Before running multiple strategies simultaneously, check their correlation.
Two strategies with correlation > 0.7 under crisis conditions are not diversified.

| Strategy Pair                        | Normal Corr | Crisis Corr | Verdict             |
|--------------------------------------|-------------|-------------|---------------------|
| GMX Basis + Uniswap LP Hedge         | 0.15        | 0.60        | ✅ Acceptable       |
| Bridge Momentum + GMX Basis          | 0.35        | 0.80        | ⚠️ Reduce sizing    |
| Stable Loop + Any directional        | -0.05       | 0.10        | ✅ True diversifier |
| Two momentum strategies (same asset) | 0.90        | 0.95        | ❌ Treat as one     |

**Rule:** If running N strategies, total portfolio risk must be calculated using
the full covariance matrix, not the sum of individual strategy risks.

---

## Section 7: Post-Trade Analysis Loop

After every strategy cycle, log and review:

```python
TRADE_LOG_SCHEMA = {
    "trade_id": str,
    "strategy": str,
    "entry_time": "ISO8601",
    "exit_time": "ISO8601",
    "entry_price": float,
    "exit_price": float,
    "position_size_usd": float,
    "predicted_slippage_bps": float,
    "actual_slippage_bps": float,         # compare vs. prediction
    "gas_cost_usd": float,
    "protocol_fees_usd": float,
    "gross_pnl_usd": float,
    "net_pnl_usd": float,                 # after all costs
    "hypothesis_confirmed": bool,         # did the thesis play out?
    "failure_mode": str,                  # if not, why not?
    "regime_at_entry": str,               # trending / mean-reverting / random / crisis
    "hurst_at_entry": float,
}
```

**Monthly Review Checklist:**
- [ ] Compare predicted vs. actual slippage across all trades
- [ ] Identify strategies with Sharpe < 0.5 for review or retirement
- [ ] Update strategy correlation matrix
- [ ] Review protocol risk scores for all active pools
- [ ] Add failed strategies to the Strategy Graveyard (Section 8)

---

## Section 8: Strategy Graveyard

Document retired strategies here. A strategy is not a failure — it's data.

| Strategy | Period | Failure Mode | Kill Reason | Lesson |
|----------|--------|--------------|-------------|--------|
| *(Add entries as strategies are retired)* | | | | |

**Rule:** No strategy may be re-deployed without reviewing its graveyard entry
and proving the failure condition no longer applies.

---

## Section 9: MVP Deployment Checklist

Before moving from paper trade to live capital:

- [ ] Backtest with realistic transaction costs (gas + slippage + protocol fees)
- [ ] Walk-forward validation on out-of-sample data (last 30d minimum)
- [ ] All kill switches implemented and tested on Arbitrum fork
- [ ] Protocol risk score ≥ 7/9 for every pool involved
- [ ] **MEV / backrun simulation:** Replay strategy transactions through a local
  mempool simulator (e.g., `mev-inspect-py` or Foundry `--unlocked` impersonation)
  to confirm a searcher cannot extract value by sandwiching or backrunning the
  entry/exit. If simulated loss > 20% of expected profit, private RPC routing
  is mandatory before going live — not optional.
- [ ] MEV exposure assessed; private RPC configured if required
- [ ] Keeper/automation trigger tested with 10+ simulated activations
- [ ] Capital allocation: start with ≤ 5% of total portfolio
- [ ] Alert system live (Telegram / PagerDuty for kill switch triggers)
- [ ] Tax logging schema active from block 0

```bash
# Arbitrum fork for final testing
anvil \
  --fork-url https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY \
  --fork-block-number latest \
  --chain-id 42161 \
  --block-time 1
```

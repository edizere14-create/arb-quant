"""
backtest.py
90-day walk-forward backtest for Strategy C (Bridge Inflow Momentum).
Run this BEFORE deploying real capital.
Outputs: win rate, Sharpe ratio, max drawdown, fee-adjusted returns.
"""

import json
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from scipy import stats
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS      = 90
ENTRY_Z            = 2.0
EXIT_Z             = 0.5
MIN_CORR_R         = 0.25
MAX_CORR_P         = 0.05
CORR_LOOKBACK      = 60
ENTRY_TIME_STOP_H  = 6
POSITION_SIZE_USD  = 150.0    # 30% of $500 portfolio
PROTOCOL_FEE_PCT   = 0.30     # Uniswap V3 30bps
SLIPPAGE_PCT       = 0.08     # 8bps estimated
GAS_COST_USD       = 0.05     # conservative Arbitrum gas


# ── Data fetchers ─────────────────────────────────────────────────────────────
def _get(url):
    try:
        r = requests.get(url, timeout=12,
                         headers={"User-Agent": "arb-quant/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] {e}")
        return None


def fetch_arb_prices() -> pd.Series:
    url  = "https://coins.llama.fi/chart/coingecko:arbitrum?span=120&period=4h"
    data = _get(url)
    if not data:
        return pd.Series(dtype=float)
    prices = data["coins"]["coingecko:arbitrum"]["prices"]
    return pd.Series(
        [p["price"] for p in prices],
        index=pd.to_datetime([p["timestamp"] for p in prices], unit="s", utc=True)
    )


def fetch_bridge_inflows() -> pd.Series:
    url  = "https://bridges.llama.fi/bridgevolume/Arbitrum?id=1"
    data = _get(url)
    if data and isinstance(data, list):
        return pd.Series(
            [d.get("depositUSD", 0) for d in data],
            index=pd.to_datetime([d.get("date", 0) for d in data], unit="s", utc=True)
        )
    # Fallback
    url  = "https://stablecoins.llama.fi/stablecoincharts/Arbitrum"
    data = _get(url)
    if data:
        s = pd.Series(
            [d.get("totalCirculatingUSD", {}).get("peggedUSD", 0) for d in data],
            index=pd.to_datetime([d.get("date", 0) for d in data], unit="s", utc=True)
        )
        return s.diff().fillna(0)
    return pd.Series(dtype=float)


# ── Signal helpers ────────────────────────────────────────────────────────────
def rolling_zscore(series: pd.Series, window: int = 30) -> pd.Series:
    mean = series.rolling(window).mean()
    std  = series.rolling(window).std()
    return (series - mean) / std.replace(0, np.nan)


def rolling_correlation(
    x: pd.Series,
    y: pd.Series,
    window: int = 60,
) -> pd.DataFrame:
    """
    Rolling Pearson r and p-value.
    Returns DataFrame with columns: r, p, valid
    """
    results = []
    for i in range(window, len(x)):
        xi = x.iloc[i-window:i].dropna()
        yi = y.iloc[i-window:i].dropna()
        idx = xi.index.intersection(yi.index)
        if len(idx) < 30:
            results.append({"r": 0.0, "p": 1.0, "valid": False})
            continue
        r, p = stats.pearsonr(xi[idx], yi[idx])
        results.append({
            "r":     round(float(r), 4),
            "p":     round(float(p), 4),
            "valid": bool(r > MIN_CORR_R and p < MAX_CORR_P),
        })
    # Pad beginning with NaN
    pad = [{"r": np.nan, "p": np.nan, "valid": False}] * window
    return pd.DataFrame(pad + results, index=x.index)


# ── Backtest engine ───────────────────────────────────────────────────────────
def run_backtest() -> dict:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"  STRATEGY C BACKTEST — {LOOKBACK_DAYS} DAYS")
    print(f"  {timestamp}")
    print(f"  Position size: ${POSITION_SIZE_USD:.0f}")
    print(f"{'='*60}")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    print("\n  Fetching price and inflow data...")
    arb_prices = fetch_arb_prices()
    inflows    = fetch_bridge_inflows()

    if len(arb_prices) < 30 or len(inflows) < 30:
        print("  ❌ Insufficient data for backtest.")
        return {}

    # ── Align to common index ─────────────────────────────────────────────────
    # Resample both to daily
    arb_daily    = arb_prices.resample("1D").last().dropna()
    inflow_daily = inflows.resample("1D").sum()
    common_idx   = arb_daily.index.intersection(inflow_daily.index)
    arb_daily    = arb_daily[common_idx]
    inflow_daily = inflow_daily[common_idx]

    # Trim to lookback
    cutoff     = arb_daily.index[-1] - pd.Timedelta(days=LOOKBACK_DAYS)
    arb_daily  = arb_daily[arb_daily.index >= cutoff]
    inflow_daily = inflow_daily[inflow_daily.index >= cutoff]

    print(f"  Data range: {arb_daily.index[0].date()} → {arb_daily.index[-1].date()}")
    print(f"  Days: {len(arb_daily)}")

    # ── Compute signals ───────────────────────────────────────────────────────
    inflow_z   = rolling_zscore(inflow_daily, 30)
    fwd_return = arb_daily.pct_change().shift(-1)   # next-day return
    corr_df    = rolling_correlation(inflow_z, fwd_return, CORR_LOOKBACK)

    # ── Walk-forward simulation ───────────────────────────────────────────────
    trades       = []
    in_trade     = False
    entry_price  = 0.0
    entry_time   = None
    entry_idx    = 0

    for i in range(len(arb_daily)):
        z       = inflow_z.iloc[i] if not np.isnan(inflow_z.iloc[i]) else 0.0
        valid   = corr_df["valid"].iloc[i] if i < len(corr_df) else False
        price   = arb_daily.iloc[i]
        date    = arb_daily.index[i]

        if not in_trade:
            # Entry condition
            if z > ENTRY_Z and valid:
                in_trade    = True
                entry_price = price
                entry_time  = date
                entry_idx   = i

        else:
            # Exit conditions
            hours_held = (date - entry_time).total_seconds() / 3600
            pnl_pct    = (price - entry_price) / entry_price * 100

            exit_reason = None
            if z < EXIT_Z:
                exit_reason = "signal_faded"
            elif hours_held >= ENTRY_TIME_STOP_H * 24:  # converted to days
                exit_reason = "time_stop"
            elif pnl_pct <= -10.0:
                exit_reason = "stop_loss"

            if exit_reason:
                # Calculate net PnL after costs
                gross_pnl    = pnl_pct
                total_cost   = (GAS_COST_USD / POSITION_SIZE_USD * 100 * 2) + \
                               (PROTOCOL_FEE_PCT * 2) + (SLIPPAGE_PCT * 2)
                net_pnl_pct  = gross_pnl - total_cost
                net_pnl_usd  = POSITION_SIZE_USD * net_pnl_pct / 100

                trades.append({
                    "entry_date":   entry_time.date(),
                    "exit_date":    date.date(),
                    "entry_price":  round(entry_price, 6),
                    "exit_price":   round(price, 6),
                    "gross_pnl_pct":round(gross_pnl, 4),
                    "cost_pct":     round(total_cost, 4),
                    "net_pnl_pct":  round(net_pnl_pct, 4),
                    "net_pnl_usd":  round(net_pnl_usd, 4),
                    "exit_reason":  exit_reason,
                    "won":          net_pnl_pct > 0,
                })
                in_trade = False

    # ── Performance metrics ───────────────────────────────────────────────────
    if not trades:
        print("\n  ⚠️  No trades triggered in backtest period.")
        print("  This means the signal (z > 2.0 AND corr valid) never fired.")
        print("  This is consistent with current live readings.")
        print(f"{'='*60}\n")
        return {"trades": 0, "signal_valid": False}

    df          = pd.DataFrame(trades)
    n_trades    = len(df)
    n_wins      = df["won"].sum()
    win_rate    = n_wins / n_trades * 100
    avg_win     = df[df["won"]]["net_pnl_pct"].mean() if n_wins > 0 else 0
    avg_loss    = df[~df["won"]]["net_pnl_pct"].mean() if (n_trades - n_wins) > 0 else 0
    total_pnl   = df["net_pnl_usd"].sum()
    returns     = df["net_pnl_pct"].values
    sharpe      = (np.mean(returns) / np.std(returns) * np.sqrt(252)) if np.std(returns) > 0 else 0

    # Max drawdown
    cumulative  = (1 + df["net_pnl_pct"] / 100).cumprod()
    rolling_max = cumulative.cummax()
    drawdown    = ((cumulative - rolling_max) / rolling_max * 100).min()

    # Yield comparison (Radiant 5.38% APY for same period)
    yield_return = POSITION_SIZE_USD * 0.0538 * (LOOKBACK_DAYS / 365)

    # Deployable verdict
    deployable = sharpe >= 0.5 and drawdown >= -30 and win_rate >= 50

    # ── Print results ─────────────────────────────────────────────────────────
    print(f"\n  {'─'*55}")
    print(f"  RESULTS")
    print(f"  {'─'*55}")
    print(f"  Trades:          {n_trades}")
    print(f"  Win rate:        {win_rate:.1f}% ({n_wins}/{n_trades})")
    print(f"  Avg win:         {avg_win:+.2f}%")
    print(f"  Avg loss:        {avg_loss:+.2f}%")
    print(f"  Total net PnL:   ${total_pnl:+.2f}")
    print(f"  Sharpe ratio:    {sharpe:.2f}")
    print(f"  Max drawdown:    {drawdown:.1f}%")
    print(f"  {'─'*55}")
    print(f"  vs. Yield only:  ${yield_return:.2f} ({LOOKBACK_DAYS}d in Radiant)")
    print(f"  {'─'*55}")

    if deployable:
        print(f"\n  ✅ VERDICT: DEPLOYABLE")
        print(f"  Sharpe ≥ 0.5, DD ≥ -30%, win rate ≥ 50%")
    else:
        fails = []
        if sharpe < 0.5:     fails.append(f"Sharpe {sharpe:.2f} < 0.5")
        if drawdown < -30:   fails.append(f"Drawdown {drawdown:.1f}% < -30%")
        if win_rate < 50:    fails.append(f"Win rate {win_rate:.1f}% < 50%")
        print(f"\n  ⚠️  VERDICT: RESEARCH ONLY — NOT DEPLOYABLE")
        for f in fails:
            print(f"  ✗ {f}")

    print(f"\n  Trade log:")
    print(f"  {'Entry':<12} {'Exit':<12} {'Gross':>8} {'Cost':>7} {'Net':>8} {'Reason'}")
    print(f"  {'-'*65}")
    for _, t in df.iterrows():
        print(f"  {str(t['entry_date']):<12} {str(t['exit_date']):<12} "
              f"{t['gross_pnl_pct']:>+7.2f}% {t['cost_pct']:>6.2f}% "
              f"{t['net_pnl_pct']:>+7.2f}%  {t['exit_reason']}")

    print(f"{'='*60}\n")

    # Save results
    results = {
        "timestamp":    timestamp,
        "n_trades":     n_trades,
        "win_rate":     round(win_rate, 2),
        "sharpe":       round(sharpe, 4),
        "max_drawdown": round(drawdown, 2),
        "total_pnl_usd":round(total_pnl, 4),
        "deployable":   deployable,
        "trades":       trades,
    }
    Path("backtest_results.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"  Results saved to backtest_results.json")
    return results


if __name__ == "__main__":
    run_backtest()

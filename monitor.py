"""
monitor.py  (v2)
Daily signal monitor with:
  - Entry signal detection (bridge z-score + correlation gate)
  - EXIT signal detection for open positions
  - Position tracker integration (positions.json)
  - Telegram alerts on entry AND exit
  - Radiant TVL sanity check
  - Gas price awareness
"""

import os
import json
import requests
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
LOG_FILE        = Path("signal_log.csv")
POSITIONS_FILE  = Path("positions.json")
CACHE_DIR       = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)

# Signal thresholds
ENTRY_Z         = 2.0
EXIT_Z          = 0.5
MIN_CORR_R      = 0.25
MAX_CORR_P      = 0.05
CORR_LOOKBACK   = 60
ENTRY_TIME_STOP = 6   # hours — max time to hold after entry

# Gas safety
MAX_GAS_COST_USD = 1.0   # if gas > $1 per trade, flag it

# Radiant TVL minimum — below this switch to Aave
MIN_RADIANT_TVL = 1_000_000   # $1M

# Telegram — load from env (set in GitHub Actions secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get(url: str, timeout: int = 10):
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "arb-quant/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        print(f"  [telegram] failed: {e}")


# ── Data fetchers ─────────────────────────────────────────────────────────────
def fetch_eth_price() -> float:
    cache = CACHE_DIR / "eth_price_current.json"
    try:
        data = _get("https://coins.llama.fi/prices/current/coingecko:ethereum")
        price = data["coins"]["coingecko:ethereum"]["price"]
        cache.write_text(json.dumps({"price": price}))
        return float(price)
    except Exception:
        if cache.exists():
            return float(json.loads(cache.read_text())["price"])
        return 2000.0


def fetch_arb_price() -> float:
    try:
        data = _get("https://coins.llama.fi/prices/current/coingecko:arbitrum")
        return float(data["coins"]["coingecko:arbitrum"]["price"])
    except Exception:
        return 0.0


def fetch_arb_prices_30d() -> pd.Series:
    cache = CACHE_DIR / "arb_prices.json"
    try:
        url = "https://coins.llama.fi/chart/coingecko:arbitrum?span=60&period=1d"
        data = _get(url)
        prices = data["coins"]["coingecko:arbitrum"]["prices"]
        series = pd.Series(
            [p["price"] for p in prices],
            index=pd.to_datetime([p["timestamp"] for p in prices], unit="s", utc=True)
        )
        cache.write_text(series.to_json())
        return series
    except Exception:
        if cache.exists():
            return pd.read_json(cache, typ="series")
        return pd.Series(dtype=float)


def fetch_bridge_inflows() -> pd.Series:
    cache = CACHE_DIR / "bridge_volume_arb.json"
    try:
        url = "https://bridges.llama.fi/bridgevolume/Arbitrum?id=1"
        data = _get(url)
        if isinstance(data, list) and len(data) > 5:
            series = pd.Series(
                [d.get("depositUSD", 0) for d in data],
                index=pd.to_datetime([d.get("date", 0) for d in data], unit="s", utc=True)
            )
            cache.write_text(series.to_json())
            return series
    except Exception:
        pass
    # Fallback: stablecoin supply delta
    try:
        url = "https://stablecoins.llama.fi/stablecoincharts/Arbitrum"
        data = _get(url)
        if data:
            series = pd.Series(
                [d.get("totalCirculatingUSD", {}).get("peggedUSD", 0) for d in data],
                index=pd.to_datetime([d.get("date", 0) for d in data], unit="s", utc=True)
            )
            return series.diff().fillna(0)
    except Exception:
        pass
    if cache.exists():
        return pd.read_json(cache, typ="series")
    return pd.Series(dtype=float)


def fetch_gas_price_gwei() -> float:
    try:
        r = requests.post("https://arb1.arbitrum.io/rpc", json={
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_gasPrice", "params": []
        }, timeout=8)
        return int(r.json()["result"], 16) / 1e9
    except Exception:
        return 0.1   # floor


def fetch_radiant_tvl() -> float:
    try:
        data = _get("https://api.llama.fi/protocol/radiant-capital")
        chain_tvls = data.get("chainTvls", {})
        for key, val in chain_tvls.items():
            if "arbitrum" in key.lower():
                if isinstance(val, list) and val:
                    return float(val[-1].get("totalLiquidityUSD", 0))
        tvl_series = data.get("tvl", [])
        if tvl_series:
            return float(tvl_series[-1].get("totalLiquidityUSD", 0))
    except Exception:
        pass
    return 0.0


# ── Signal computation ────────────────────────────────────────────────────────
def compute_zscore(series: pd.Series, window: int = 30) -> float:
    if len(series) < window:
        return 0.0
    recent = series.iloc[-window:]
    std = recent.std()
    if std == 0:
        return 0.0
    return float((series.iloc[-1] - recent.mean()) / std)


def inflow_signal_is_valid(
    inflow_z: pd.Series,
    asset_fwd_return: pd.Series,
    lookback: int = 60,
) -> tuple[bool, float, float]:
    """Returns (valid, r, p)"""
    x = inflow_z.iloc[-lookback:]
    y = asset_fwd_return.iloc[-lookback:]
    valid_mask = x.notna() & y.notna()
    if valid_mask.sum() < 30:
        return False, 0.0, 1.0
    r, p = stats.pearsonr(x[valid_mask], y[valid_mask])
    return bool(r > MIN_CORR_R and p < MAX_CORR_P), round(float(r), 4), round(float(p), 4)


# ── Position tracker ──────────────────────────────────────────────────────────
def load_positions() -> dict:
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text())
        except Exception:
            pass
    return {"open": [], "closed": []}


def save_positions(positions: dict) -> None:
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2, default=str))


def open_position(asset: str, entry_price: float, size_usd: float,
                  entry_z: float, strategy: str = "C") -> dict:
    pos = {
        "id":           f"{asset}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "asset":        asset,
        "strategy":     strategy,
        "entry_time":   datetime.now(timezone.utc).isoformat(),
        "entry_price":  entry_price,
        "size_usd":     size_usd,
        "entry_z":      entry_z,
        "status":       "open",
        "exit_time":    None,
        "exit_price":   None,
        "exit_reason":  None,
        "pnl_usd":      None,
        "pnl_pct":      None,
    }
    positions = load_positions()
    positions["open"].append(pos)
    save_positions(positions)
    return pos


def close_position(pos_id: str, exit_price: float, exit_reason: str) -> dict | None:
    positions = load_positions()
    for i, pos in enumerate(positions["open"]):
        if pos["id"] == pos_id:
            pos["exit_time"]  = datetime.now(timezone.utc).isoformat()
            pos["exit_price"] = exit_price
            pos["exit_reason"]= exit_reason
            pos["status"]     = "closed"
            pnl_pct           = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
            pos["pnl_pct"]    = round(pnl_pct, 4)
            pos["pnl_usd"]    = round(pos["size_usd"] * pnl_pct / 100, 4)
            positions["closed"].append(pos)
            positions["open"].pop(i)
            save_positions(positions)
            return pos
    return None


def check_exit_conditions(positions: dict, current_z: float,
                          current_price: float) -> list[dict]:
    """
    Check all open positions for exit conditions:
    1. z-score dropped below EXIT_Z (signal faded)
    2. Time stop: position held > ENTRY_TIME_STOP hours
    3. Stop loss: position down > 10%
    Returns list of positions that should be closed.
    """
    to_close = []
    now = datetime.now(timezone.utc)

    for pos in positions["open"]:
        reasons = []

        # Exit condition 1: z-score faded
        if current_z < EXIT_Z:
            reasons.append(f"z={current_z:.4f} < {EXIT_Z} (signal faded)")

        # Exit condition 2: time stop
        try:
            entry_dt = datetime.fromisoformat(pos["entry_time"])
            hours_held = (now - entry_dt).total_seconds() / 3600
            if hours_held >= ENTRY_TIME_STOP:
                reasons.append(f"time stop ({hours_held:.1f}h >= {ENTRY_TIME_STOP}h)")
        except Exception:
            pass

        # Exit condition 3: stop loss > 10%
        if current_price > 0 and pos.get("entry_price", 0) > 0:
            pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
            if pnl_pct <= -10.0:
                reasons.append(f"stop loss ({pnl_pct:.1f}%)")

        if reasons:
            to_close.append({"position": pos, "reasons": reasons})

    return to_close


# ── Yield router health check ─────────────────────────────────────────────────
def check_yield_router_health() -> dict:
    """
    Verify Radiant TVL is adequate before recommending it.
    Switch recommendation to Aave if TVL too thin.
    """
    tvl = fetch_radiant_tvl()
    if tvl < MIN_RADIANT_TVL:
        return {
            "recommended": "Aave V3",
            "reason": f"Radiant TVL ${tvl:,.0f} below ${MIN_RADIANT_TVL:,.0f} minimum",
            "url": "app.aave.com",
            "radiant_tvl": tvl,
        }
    return {
        "recommended": "Radiant",
        "reason": f"Radiant TVL ${tvl:,.0f} — adequate",
        "url": "app.radiant.capital",
        "radiant_tvl": tvl,
    }


# ── Gas check ─────────────────────────────────────────────────────────────────
def check_gas(eth_price: float, position_size_usd: float = 500.0) -> dict:
    gwei        = fetch_gas_price_gwei()
    gas_cost    = gwei * 300_000 * 1e-9 * eth_price
    gas_pct     = gas_cost / position_size_usd * 100
    acceptable  = gas_cost <= MAX_GAS_COST_USD
    return {
        "gwei":         round(gwei, 4),
        "cost_usd":     round(gas_cost, 4),
        "cost_pct":     round(gas_pct, 4),
        "acceptable":   acceptable,
    }


# ── Logger ────────────────────────────────────────────────────────────────────
def log_signal(timestamp, z_score, pearson_r, p_value,
               corr_valid, entry_signal, decision) -> None:
    row = pd.DataFrame([{
        "timestamp":    timestamp,
        "z_score":      round(z_score, 4),
        "pearson_r":    round(pearson_r, 4),
        "p_value":      round(p_value, 4),
        "corr_valid":   corr_valid,
        "entry_signal": entry_signal,
        "decision":     decision,
    }])
    header = not LOG_FILE.exists()
    row.to_csv(LOG_FILE, mode="a", header=header, index=False)


# ── Main ──────────────────────────────────────────────────────────────────────
def run_monitor():
    now = datetime.now(timezone.utc)
    ts  = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n[monitor] {ts}")

    # ── Fetch data ────────────────────────────────────────────────────────────
    eth_price    = fetch_eth_price()
    arb_price    = fetch_arb_price()
    inflows      = fetch_bridge_inflows()
    arb_prices   = fetch_arb_prices_30d()

    # ── Compute signal ────────────────────────────────────────────────────────
    z_score = compute_zscore(inflows) if len(inflows) >= 10 else 0.0

    fwd_returns = arb_prices.pct_change().shift(-1).dropna()
    inflow_z_aligned = pd.Series(
        np.interp(
            np.linspace(0, 1, len(fwd_returns)),
            np.linspace(0, 1, max(len(inflows), 1)),
            inflows.values if len(inflows) > 0 else [0]
        )
    )
    inflow_z_series = (inflow_z_aligned - inflow_z_aligned.mean()) / (inflow_z_aligned.std() + 1e-9)

    corr_valid, pearson_r, p_value = inflow_signal_is_valid(
        inflow_z_series, fwd_returns
    )

    entry_signal = z_score > ENTRY_Z and corr_valid

    # ── Gas check ─────────────────────────────────────────────────────────────
    gas = check_gas(eth_price)
    if not gas["acceptable"]:
        print(f"  ⚠️  Gas high: {gas['gwei']:.3f} gwei (${gas['cost_usd']:.3f}/trade)")

    # ── Yield router health ───────────────────────────────────────────────────
    yield_health = check_yield_router_health()
    if "Aave" in yield_health["recommended"]:
        print(f"  ⚠️  Yield router: {yield_health['reason']} → switching to Aave")

    # ── Check open positions for exits ────────────────────────────────────────
    positions    = load_positions()
    exit_targets = check_exit_conditions(positions, z_score, arb_price)

    for target in exit_targets:
        pos     = target["position"]
        reasons = target["reasons"]
        closed  = close_position(pos["id"], arb_price, " | ".join(reasons))
        if closed:
            msg = (
                f"🔴 *EXIT SIGNAL — Strategy {pos['strategy']}*\n"
                f"Asset: {pos['asset']}\n"
                f"PnL: {closed['pnl_pct']:+.2f}% (${closed['pnl_usd']:+.4f})\n"
                f"Reason: {' | '.join(reasons)}\n"
                f"→ Return capital to yield router"
            )
            print(f"  🔴 EXIT: {pos['asset']} {closed['pnl_pct']:+.2f}% — {' | '.join(reasons)}")
            send_telegram(msg)

    # ── Entry decision ────────────────────────────────────────────────────────
    if entry_signal:
        decision = "ENTRY SIGNAL"
        msg = (
            f"⚡ *ENTRY SIGNAL — Strategy C*\n"
            f"z={z_score:.4f} r={pearson_r:.4f} p={p_value:.4f}\n"
            f"ARB: ${arb_price:.4f}\n"
            f"Gas: {gas['gwei']:.3f} gwei (${gas['cost_usd']:.4f})\n"
            f"→ Run `trade_executor.py` before touching MetaMask"
        )
        print(f"  ⚡ ENTRY SIGNAL — z={z_score:.4f} r={pearson_r:.4f} p={p_value:.4f}")
        send_telegram(msg)
    else:
        decision = "PARKED"
        reason   = f"z={z_score:.4f} < {ENTRY_Z}" if z_score <= ENTRY_Z \
                   else f"corr decayed (r={pearson_r:.4f}, p={p_value:.4f})"
        print(f"  ⏸ PARKED — recheck tomorrow  [{reason}]")

        # Send daily summary to Telegram (silent — no alert tone)
        if TELEGRAM_BOT_TOKEN:
            send_telegram(
                f"📊 *Daily Monitor — {now.strftime('%Y-%m-%d')}*\n"
                f"Status: PARKED\n"
                f"z={z_score:.4f}  r={pearson_r:.4f}  p={p_value:.4f}\n"
                f"ARB: ${arb_price:.4f}  ETH: ${eth_price:.0f}\n"
                f"Yield: {yield_health['recommended']} — {yield_health['reason'][:40]}"
            )

    # ── Log ───────────────────────────────────────────────────────────────────
    log_signal(ts, z_score, pearson_r, p_value, corr_valid, entry_signal, decision)
    print(f"  📝 Logged to {LOG_FILE}")

    return {
        "timestamp":    ts,
        "z_score":      z_score,
        "pearson_r":    pearson_r,
        "p_value":      p_value,
        "corr_valid":   corr_valid,
        "entry_signal": entry_signal,
        "decision":     decision,
        "arb_price":    arb_price,
        "eth_price":    eth_price,
        "gas":          gas,
        "yield_health": yield_health,
        "exits":        len(exit_targets),
    }


if __name__ == "__main__":
    run_monitor()

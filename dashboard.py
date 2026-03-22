"""
dashboard.py
Streamlit live dashboard for arb-quant system.
Run locally:  streamlit run dashboard.py
Deploy free:  streamlit.io/cloud → connect GitHub repo → select dashboard.py
"""

import streamlit as st
import pandas as pd
import numpy as np
import requests
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ARB Quant Monitor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@300;400;600&display=swap');

    .stApp { background-color: #0a0a0f; }

    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
        color: #e0e0e0;
    }

    .main-title {
        font-family: 'JetBrains Mono', monospace;
        font-size: 2rem;
        font-weight: 700;
        color: #00ff88;
        letter-spacing: -0.02em;
        margin-bottom: 0;
    }

    .subtitle {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.75rem;
        color: #444;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        margin-top: 0;
    }

    .metric-card {
        background: #111118;
        border: 1px solid #1e1e2e;
        border-radius: 8px;
        padding: 20px;
        margin-bottom: 12px;
    }

    .metric-label {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.65rem;
        color: #555;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        margin-bottom: 6px;
    }

    .metric-value {
        font-family: 'JetBrains Mono', monospace;
        font-size: 1.8rem;
        font-weight: 700;
        line-height: 1;
    }

    .metric-sub {
        font-size: 0.75rem;
        color: #666;
        margin-top: 4px;
    }

    .go    { color: #00ff88; }
    .caution { color: #ffaa00; }
    .nogo  { color: #ff4455; }
    .neutral { color: #888; }

    .signal-banner {
        border-radius: 8px;
        padding: 16px 24px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 1rem;
        font-weight: 700;
        text-align: center;
        letter-spacing: 0.05em;
        margin-bottom: 20px;
    }

    .banner-parked {
        background: #111118;
        border: 1px solid #222;
        color: #666;
    }

    .banner-entry {
        background: #001a0d;
        border: 1px solid #00ff88;
        color: #00ff88;
        box-shadow: 0 0 20px rgba(0,255,136,0.15);
    }

    .banner-caution {
        background: #1a1200;
        border: 1px solid #ffaa00;
        color: #ffaa00;
    }

    .log-row {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.75rem;
        padding: 8px 12px;
        border-bottom: 1px solid #1a1a24;
        display: flex;
        justify-content: space-between;
    }

    .section-header {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.65rem;
        color: #333;
        text-transform: uppercase;
        letter-spacing: 0.15em;
        border-bottom: 1px solid #1a1a24;
        padding-bottom: 8px;
        margin-bottom: 16px;
        margin-top: 32px;
    }

    div[data-testid="stMetric"] {
        background: #111118;
        border: 1px solid #1e1e2e;
        border-radius: 8px;
        padding: 16px;
    }

    div[data-testid="stMetric"] label {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.65rem !important;
        color: #555 !important;
        text-transform: uppercase;
        letter-spacing: 0.1em;
    }

    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        font-family: 'JetBrains Mono', monospace;
        font-size: 1.6rem !important;
        color: #e0e0e0;
    }

    .stButton > button {
        background: #00ff88;
        color: #000;
        font-family: 'JetBrains Mono', monospace;
        font-weight: 700;
        font-size: 0.8rem;
        border: none;
        border-radius: 4px;
        padding: 10px 24px;
        letter-spacing: 0.05em;
        width: 100%;
    }

    .stButton > button:hover {
        background: #00cc6a;
    }

    .timestamp {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.65rem;
        color: #333;
    }
</style>
""", unsafe_allow_html=True)


# ── Data fetchers ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def fetch_eth_prices() -> pd.Series:
    """Fetch 30d ETH price from DeFiLlama."""
    try:
        url = "https://coins.llama.fi/chart/coingecko:ethereum?span=30&period=1d"
        r = requests.get(url, timeout=10)
        data = r.json()["coins"]["coingecko:ethereum"]["prices"]
        prices = pd.Series(
            [p["price"] for p in data],
            index=pd.to_datetime([p["timestamp"] for p in data], unit="s", utc=True)
        )
        return prices
    except Exception:
        return pd.Series(dtype=float)


@st.cache_data(ttl=3600)
def fetch_arb_prices() -> pd.Series:
    """Fetch 30d ARB price from DeFiLlama."""
    try:
        url = "https://coins.llama.fi/chart/coingecko:arbitrum?span=30&period=1d"
        r = requests.get(url, timeout=10)
        data = r.json()["coins"]["coingecko:arbitrum"]["prices"]
        prices = pd.Series(
            [p["price"] for p in data],
            index=pd.to_datetime([p["timestamp"] for p in data], unit="s", utc=True)
        )
        return prices
    except Exception:
        return pd.Series(dtype=float)


@st.cache_data(ttl=3600)
def fetch_bridge_volume() -> pd.Series:
    """Fetch Arbitrum bridge inflow volume."""
    try:
        url = "https://bridges.llama.fi/bridgevolume/Arbitrum?id=1"
        r = requests.get(url, timeout=10)
        data = r.json()
        if isinstance(data, list):
            series = pd.Series(
                [d.get("depositUSD", 0) for d in data[-30:]],
                index=pd.to_datetime([d.get("date", 0) for d in data[-30:]], unit="s", utc=True)
            )
            return series
    except Exception:
        pass
    # Fallback: stablecoin supply proxy
    try:
        url = "https://stablecoins.llama.fi/stablecoincharts/Arbitrum"
        r = requests.get(url, timeout=10)
        data = r.json()
        series = pd.Series(
            [d.get("totalCirculatingUSD", {}).get("peggedUSD", 0) for d in data[-30:]],
            index=pd.to_datetime([d.get("date", 0) for d in data[-30:]], unit="s", utc=True)
        )
        return series.diff().fillna(0)
    except Exception:
        return pd.Series(dtype=float)


@st.cache_data(ttl=3600)
def fetch_yield_apys() -> dict:
    """Fetch current APYs from DeFiLlama yields."""
    results = {"Aave V3": None, "Radiant": None, "Yearn V3": None}
    try:
        r = requests.get("https://yields.llama.fi/pools", timeout=12)
        pools = r.json().get("data", [])
        keywords = {
            "Aave V3":  ("aave-v3",   "arbitrum", "usdc"),
            "Radiant":  ("radiant",   "arbitrum", "usdc"),
            "Yearn V3": ("yearn",     "arbitrum", "usdc"),
        }
        for name, (project, chain, symbol) in keywords.items():
            match = next(
                (p for p in pools
                 if project in p.get("project", "").lower()
                 and chain in p.get("chain", "").lower()
                 and symbol in p.get("symbol", "").lower()),
                None
            )
            if match:
                results[name] = {
                    "apy": match.get("apy") or match.get("apyBase") or 0,
                    "tvl": match.get("tvlUsd") or 0,
                }
    except Exception:
        pass
    return results


def compute_hurst(ts: np.ndarray) -> float:
    """Hurst exponent via R/S analysis. Cap lags at len//4."""
    if len(ts) < 20:
        return 0.5
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
    if len(rs_values) < 2:
        return 0.5
    log_lags = np.log(list(lags)[:len(rs_values)])
    log_rs   = np.log(rs_values)
    return float(np.polyfit(log_lags, log_rs, 1)[0])


def compute_zscore(series: pd.Series, window: int = 30) -> float:
    """Rolling z-score of latest value."""
    if len(series) < window:
        return 0.0
    recent = series.iloc[-window:]
    mean   = recent.mean()
    std    = recent.std()
    if std == 0:
        return 0.0
    return float((series.iloc[-1] - mean) / std)


def classify_regime(H: float, rv: float, rv_zscore: float) -> tuple[str, str]:
    """Returns (regime_name, css_class)."""
    if rv_zscore > 2.0:
        return "CRISIS / SHOCK", "nogo"
    if H > 0.6:
        return "TRENDING", "go"
    if H < 0.4:
        return "MEAN-REVERTING", "caution"
    return "RANDOM WALK", "neutral"


def load_signal_log() -> pd.DataFrame:
    """Load signal_log.csv if it exists."""
    log_path = Path("signal_log.csv")
    if log_path.exists():
        try:
            df = pd.read_csv(log_path)
            return df.tail(30)
        except Exception:
            pass
    # Return empty demo log
    return pd.DataFrame(columns=["timestamp", "z_score", "r_value", "p_value", "decision"])


# ── Main dashboard ────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Header
    st.markdown('<p class="main-title">⚡ ARB QUANT MONITOR</p>', unsafe_allow_html=True)
    st.markdown(f'<p class="subtitle">Arbitrum DeFi Signal System &nbsp;·&nbsp; {now}</p>', unsafe_allow_html=True)

    # Refresh button
    col_refresh, col_space = st.columns([1, 5])
    with col_refresh:
        if st.button("↺  REFRESH"):
            st.cache_data.clear()
            st.rerun()

    st.markdown("---")

    # ── Fetch all data ────────────────────────────────────────────────────────
    with st.spinner("Fetching live data..."):
        eth_prices   = fetch_eth_prices()
        arb_prices   = fetch_arb_prices()
        bridge_vol   = fetch_bridge_volume()
        yield_apys   = fetch_yield_apys()

    # ── Compute metrics ───────────────────────────────────────────────────────
    H        = 0.5
    rv       = 0.0
    rv_z     = 0.0
    z_score  = 0.0

    if len(eth_prices) >= 10:
        log_returns = np.log(eth_prices / eth_prices.shift(1)).dropna().values
        H           = compute_hurst(log_returns)
        rv          = float(np.std(log_returns) * np.sqrt(365) * 100)
        rv_series   = pd.Series(log_returns)
        rv_z        = compute_zscore(rv_series)

    if len(bridge_vol) >= 10:
        z_score = compute_zscore(bridge_vol)

    regime, regime_css = classify_regime(H, rv, rv_z)

    # Signal decision
    corr_valid   = False   # would need scipy — simplified here
    signal_fires = z_score > 2.0

    if signal_fires:
        banner_class = "banner-entry"
        banner_text  = "⚡ ENTRY SIGNAL — run trade_executor.py before touching MetaMask"
    else:
        banner_class = "banner-parked"
        banner_text  = f"⏸  PARKED — z={z_score:.4f} < 2.0 — capital in yield"

    st.markdown(
        f'<div class="signal-banner {banner_class}">{banner_text}</div>',
        unsafe_allow_html=True,
    )

    # ── Row 1: Core metrics ───────────────────────────────────────────────────
    st.markdown('<div class="section-header">Market Regime</div>', unsafe_allow_html=True)

    c1, c2, c3, c4 = st.columns(4)

    with c1:
        color = "go" if H > 0.6 else ("caution" if H < 0.4 else "neutral")
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Hurst Exponent</div>
            <div class="metric-value {color}">{H:.4f}</div>
            <div class="metric-sub">{"Trending" if H > 0.6 else "Mean-Rev" if H < 0.4 else "Random"}</div>
        </div>""", unsafe_allow_html=True)

    with c2:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">30d Realized Vol</div>
            <div class="metric-value">{rv:.1f}%</div>
            <div class="metric-sub">Annualized (ETH)</div>
        </div>""", unsafe_allow_html=True)

    with c3:
        color = "nogo" if rv_z > 2.0 else "neutral"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Vol Z-Score</div>
            <div class="metric-value {color}">{rv_z:.2f}</div>
            <div class="metric-sub">>2σ = crisis zone</div>
        </div>""", unsafe_allow_html=True)

    with c4:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Regime</div>
            <div class="metric-value {regime_css}" style="font-size:1.1rem;margin-top:6px">{regime}</div>
            <div class="metric-sub">Strategy C active</div>
        </div>""", unsafe_allow_html=True)

    # ── Row 2: Signal metrics ─────────────────────────────────────────────────
    st.markdown('<div class="section-header">Bridge Inflow Signal (Strategy C)</div>', unsafe_allow_html=True)

    s1, s2, s3 = st.columns(3)

    with s1:
        z_color = "go" if z_score > 2.0 else ("caution" if z_score > 1.0 else "neutral")
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Bridge Z-Score</div>
            <div class="metric-value {z_color}">{z_score:.4f}</div>
            <div class="metric-sub">Entry threshold: > 2.0</div>
        </div>""", unsafe_allow_html=True)

    with s2:
        corr_color = "nogo"
        corr_text  = "DECAYED"
        corr_sub   = "r=N/A (need more data)"
        try:
            from scipy import stats as _stats
            if len(bridge_vol) >= 30 and len(arb_prices) >= 30:
                fwd_ret  = arb_prices.pct_change().shift(-1).dropna()
                inflow_s = bridge_vol.reindex(fwd_ret.index, method="nearest").fillna(0)
                mean_i   = inflow_s.rolling(30).mean()
                std_i    = inflow_s.rolling(30).std().replace(0, float("nan"))
                z_s      = ((inflow_s - mean_i) / std_i).dropna()
                common   = z_s.index.intersection(fwd_ret.index)
                if len(common) >= 30:
                    r, p = _stats.pearsonr(z_s[common], fwd_ret[common])
                    valid      = bool(r > 0.25 and p < 0.05)
                    corr_color = "go" if valid else "nogo"
                    corr_text  = "VALID" if valid else "DECAYED"
                    corr_sub   = f"r={r:+.4f} p={p:.4f} (need r>0.25, p<0.05)"
        except Exception:
            pass
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Correlation Gate</div>
            <div class="metric-value {corr_color}">{corr_text}</div>
            <div class="metric-sub">{corr_sub}</div>
        </div>""", unsafe_allow_html=True)

    with s3:
        arb_price = float(arb_prices.iloc[-1]) if len(arb_prices) > 0 else 0
        arb_chg   = float((arb_prices.iloc[-1] / arb_prices.iloc[-2] - 1) * 100) if len(arb_prices) > 1 else 0
        chg_color = "go" if arb_chg > 0 else "nogo"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">ARB Price</div>
            <div class="metric-value">${arb_price:.3f}</div>
            <div class="metric-sub {chg_color}">{arb_chg:+.2f}% 24h</div>
        </div>""", unsafe_allow_html=True)

    # ── Row 3: Yield router ───────────────────────────────────────────────────
    st.markdown('<div class="section-header">Yield Router (Strategy D — Idle Capital)</div>', unsafe_allow_html=True)

    RISK_DISCOUNT = {"Aave V3": 0.0, "Radiant": 0.3, "Yearn V3": 0.5}
    y_cols = st.columns(3)
    best_apy  = 0
    best_name = "Aave V3"

    for i, (name, data) in enumerate(yield_apys.items()):
        with y_cols[i]:
            if data:
                apy      = data["apy"]
                adj_apy  = apy - RISK_DISCOUNT.get(name, 0)
                tvl      = data["tvl"]
                daily    = 500 * (apy / 100) / 365
                if adj_apy > best_apy:
                    best_apy  = adj_apy
                    best_name = name
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">{name}</div>
                    <div class="metric-value go">{apy:.2f}%</div>
                    <div class="metric-sub">
                        Adj: {adj_apy:.2f}% · TVL ${tvl/1e6:.1f}M<br>
                        Daily on $500: ${daily:.3f}
                    </div>
                </div>""", unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">{name}</div>
                    <div class="metric-value neutral">N/A</div>
                    <div class="metric-sub">Data unavailable</div>
                </div>""", unsafe_allow_html=True)

    st.markdown(
        f'<div class="signal-banner banner-parked" style="margin-top:12px">'
        f'🏆 &nbsp; Deploy idle capital to: <strong>{best_name}</strong> '
        f'({best_apy:.2f}% risk-adjusted APY)'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Row 4: ETH price chart ────────────────────────────────────────────────
    st.markdown('<div class="section-header">ETH Price — 30 Day</div>', unsafe_allow_html=True)

    if len(eth_prices) > 0:
        chart_df = pd.DataFrame({"ETH Price (USD)": eth_prices})
        st.line_chart(chart_df, color=["#00ff88"])

    # ── Row 5: Signal log ─────────────────────────────────────────────────────
    st.markdown('<div class="section-header">Signal Log (Last 30 Entries)</div>', unsafe_allow_html=True)

    log_df = load_signal_log()

    if log_df.empty:
        st.markdown(
            '<div class="metric-card neutral" style="text-align:center;padding:32px">'
            'No signal log found. Run <code>monitor.py</code> to start logging.'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        def color_decision(val):
            if "ENTRY" in str(val):
                return "color: #00ff88; font-weight: bold"
            return "color: #555"
        styled = log_df.style.applymap(color_decision, subset=["decision"] if "decision" in log_df.columns else [])
        st.dataframe(styled, use_container_width=True, hide_index=True)

    # ── Row 6: Portfolio PnL ──────────────────────────────────────────────────
    st.markdown('<div class="section-header">Portfolio Performance</div>', unsafe_allow_html=True)

    positions_data = {"open": [], "closed": []}
    try:
        pos_file = Path("positions.json")
        if pos_file.exists():
            positions_data = json.loads(pos_file.read_text())
    except Exception:
        pass

    closed = positions_data.get("closed", [])
    open_p = positions_data.get("open", [])

    p1, p2, p3, p4 = st.columns(4)

    total_pnl    = sum(t.get("pnl_usd", 0) or 0 for t in closed)
    n_trades     = len(closed)
    n_wins       = sum(1 for t in closed if (t.get("pnl_pct") or 0) > 0)
    win_rate     = (n_wins / n_trades * 100) if n_trades > 0 else 0
    returns      = [t.get("pnl_pct", 0) or 0 for t in closed]
    sharpe       = (np.mean(returns) / np.std(returns) * np.sqrt(252)) \
                   if len(returns) > 1 and np.std(returns) > 0 else 0

    with p1:
        pnl_color = "go" if total_pnl >= 0 else "nogo"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Total Net PnL</div>
            <div class="metric-value {pnl_color}">${total_pnl:+.2f}</div>
            <div class="metric-sub">{n_trades} closed trades</div>
        </div>""", unsafe_allow_html=True)

    with p2:
        wr_color = "go" if win_rate >= 50 else "nogo"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Win Rate</div>
            <div class="metric-value {wr_color}">{win_rate:.1f}%</div>
            <div class="metric-sub">{n_wins} wins / {n_trades} trades</div>
        </div>""", unsafe_allow_html=True)

    with p3:
        sh_color = "go" if sharpe >= 1.0 else ("caution" if sharpe >= 0.5 else "nogo")
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Sharpe Ratio</div>
            <div class="metric-value {sh_color}">{sharpe:.2f}</div>
            <div class="metric-sub">Target ≥ 1.0</div>
        </div>""", unsafe_allow_html=True)

    with p4:
        n_open = len(open_p)
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Open Positions</div>
            <div class="metric-value {'caution' if n_open > 0 else 'neutral'}">{n_open}</div>
            <div class="metric-sub">{'Active trade running' if n_open > 0 else 'No open positions'}</div>
        </div>""", unsafe_allow_html=True)

    # Open positions detail
    if open_p:
        st.markdown('<div class="section-header">Open Positions</div>', unsafe_allow_html=True)
        open_df = pd.DataFrame(open_p)[
            ["id", "asset", "strategy", "entry_time", "entry_price", "size_usd"]
        ]
        st.dataframe(open_df, use_container_width=True, hide_index=True)

    # Closed trades table
    if closed:
        st.markdown('<div class="section-header">Closed Trades</div>', unsafe_allow_html=True)
        closed_df = pd.DataFrame(closed)[
            ["entry_date", "exit_date", "asset", "pnl_pct", "pnl_usd", "exit_reason"]
        ] if all(k in closed[0] for k in ["entry_date", "pnl_pct"]) else pd.DataFrame(closed)

        def color_pnl(val):
            try:
                return "color: #00ff88" if float(val) > 0 else "color: #ff4455"
            except Exception:
                return ""

        if "pnl_pct" in closed_df.columns:
            styled_c = closed_df.style.applymap(color_pnl, subset=["pnl_pct", "pnl_usd"])
            st.dataframe(styled_c, use_container_width=True, hide_index=True)
        else:
            st.dataframe(closed_df, use_container_width=True, hide_index=True)

    # ── Row 7: GMX Signal ─────────────────────────────────────────────────────
    st.markdown('<div class="section-header">GMX Funding Rate Signal (Strategy A)</div>', unsafe_allow_html=True)

    gmx_cache = Path(".cache/gmx_signal.json")
    if gmx_cache.exists():
        try:
            gmx_data = json.loads(gmx_cache.read_text())
            spread   = gmx_data.get("spread", {})
            g1, g2, g3 = st.columns(3)
            with g1:
                net_bps = spread.get("net_spread_bps", 0)
                g_color = "go" if net_bps >= 15 else "neutral"
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Net Spread</div>
                    <div class="metric-value {g_color}">{net_bps:.1f} bps</div>
                    <div class="metric-sub">Entry threshold: 15 bps</div>
                </div>""", unsafe_allow_html=True)
            with g2:
                gmx_r = gmx_data.get("gmx_rate", {}).get("rate_8h", 0)
                cex_r = gmx_data.get("cex_rate", {}).get("rate_8h", 0)
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">GMX vs CEX Rate (8h)</div>
                    <div class="metric-value neutral">{gmx_r:+.4f}% / {cex_r:+.4f}%</div>
                    <div class="metric-sub">GMX / Binance</div>
                </div>""", unsafe_allow_html=True)
            with g3:
                decision = gmx_data.get("decision", "NO SIGNAL")
                d_color  = "go" if "ENTRY" in decision else "neutral"
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Decision</div>
                    <div class="metric-value {d_color}" style="font-size:1rem;margin-top:8px">{decision}</div>
                    <div class="metric-sub">{gmx_data.get('timestamp','')[:16]}</div>
                </div>""", unsafe_allow_html=True)
        except Exception:
            st.markdown('<div class="metric-card neutral">Run gmx_signal.py to populate</div>', unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="metric-card neutral" style="text-align:center;padding:20px">'
            'Run <code>python gmx_signal.py</code> to populate GMX signal data.'
            '</div>',
            unsafe_allow_html=True,
        )

    # ── Footer ────────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown(
        '<p class="timestamp">Data: DeFiLlama · Refreshes every hour · '
        'Run monitor.py daily for full correlation check · '
        'github.com/edizere14-create/arb-quant</p>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()

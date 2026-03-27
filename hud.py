"""
hud.py  (v6.0 — God-Tier Edition)
Streamlit dashboard: real-time Z-scores, Correlation charts, God-Signal conviction meter.
Run: streamlit run hud.py
"""

import json
import importlib.util
import sqlite3
import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config as cfg

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ARB Alpha Operator v6.0",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom CSS (carried forward from dashboard.py) ────────────────────────────
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

    .mode-badge {
        display: inline-block;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        padding: 8px 12px;
        border-radius: 999px;
        margin: 6px 0 14px 0;
    }

    .mode-test {
        background: #1f1400;
        color: #ffaa00;
        border: 1px solid #ffaa00;
        box-shadow: 0 0 16px rgba(255,170,0,0.12);
    }

    .mode-live {
        background: #0d1411;
        color: #00ff88;
        border: 1px solid #00ff88;
        box-shadow: 0 0 16px rgba(0,255,136,0.10);
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

    .banner-fire {
        background: #001a0d;
        border: 1px solid #00ff88;
        color: #00ff88;
        box-shadow: 0 0 20px rgba(0,255,136,0.15);
    }

    .banner-hold {
        background: #111118;
        border: 1px solid #222;
        color: #666;
    }

    .banner-halt {
        background: #1a0000;
        border: 1px solid #ff4455;
        color: #ff4455;
        box-shadow: 0 0 20px rgba(255,68,85,0.15);
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

    .conviction-meter {
        text-align: center;
        padding: 32px 16px;
    }

    .conviction-score {
        font-family: 'JetBrains Mono', monospace;
        font-size: 4rem;
        font-weight: 700;
        line-height: 1;
    }

    .conviction-label {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.75rem;
        color: #555;
        text-transform: uppercase;
        letter-spacing: 0.15em;
        margin-top: 8px;
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

    .stButton > button:hover { background: #00cc6a; }

    .hb-ok   { color: #00ff88; }
    .hb-warn { color: #ffaa00; }
    .hb-fail { color: #ff4455; }
</style>
""", unsafe_allow_html=True)


# ── Data loaders (from .cache/ JSON + SQLite) ─────────────────────────────────
CACHE = Path(".cache")

def _load_json(name: str) -> dict[str, Any]:
    f = CACHE / name
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def _default_portfolio() -> dict[str, Any]:
    return {
        "total_value": cfg.PORTFOLIO_USD,
        "cash": cfg.PORTFOLIO_USD,
        "positions_value": 0.0,
        "drawdown_pct": 0.0,
        "sharpe_rolling": None,
    }


def _load_shadow_trades(limit: int = 20) -> pd.DataFrame:
    db_path = Path("shadow_trades.db")
    if not db_path.exists():
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(str(db_path))
        df = pd.read_sql_query(
            f"SELECT * FROM trades ORDER BY id DESC LIMIT {int(limit)}", conn,
        )
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


def _load_equity_curve() -> pd.DataFrame:
    db_path = Path("shadow_trades.db")
    if not db_path.exists():
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(str(db_path))
        df = pd.read_sql_query(
            "SELECT timestamp, total_value, drawdown_pct, sharpe_rolling "
            "FROM portfolio_snapshots ORDER BY id ASC", conn,
        )
        conn.close()
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception:
        return pd.DataFrame()


def _render_corr_matrix(pivot: pd.DataFrame) -> None:
    if importlib.util.find_spec("matplotlib") is None:
        st.dataframe(pivot.round(4), width="stretch")
        return

    styled = pivot.style.background_gradient(cmap="RdYlGn", vmin=-1, vmax=1)
    st.dataframe(styled, width="stretch")


# ── Main dashboard ────────────────────────────────────────────────────────────
def main() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    consensus_threshold = cfg.TEST_CONSENSUS_THRESHOLD if cfg.TEST_MODE else cfg.CONSENSUS_THRESHOLD
    bridge_threshold = cfg.TEST_BRIDGE_Z_THRESHOLD if cfg.TEST_MODE else cfg.BRIDGE_Z_THRESHOLD
    sentiment_threshold = cfg.TEST_SENTIMENT_THRESHOLD if cfg.TEST_MODE else cfg.SENTIMENT_THRESHOLD
    mode_class = "mode-test" if cfg.TEST_MODE else "mode-live"
    mode_label = "TEST MODE" if cfg.TEST_MODE else "LIVE MODE"

    st.markdown('<p class="main-title">&#9889; ARBITRUM ALPHA OPERATOR v6.0</p>', unsafe_allow_html=True)
    st.markdown(f'<p class="subtitle">God-Tier Edition &nbsp;&middot;&nbsp; {now}</p>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="mode-badge {mode_class}">{mode_label} &middot; '
        f'Consensus &gt; {consensus_threshold:.2f} &middot; '
        f'Bridge Z &gt; {bridge_threshold:.2f} &middot; '
        f'Sentiment &gt; {sentiment_threshold:.2f}</div>',
        unsafe_allow_html=True,
    )

    col_r, _ = st.columns([1, 5])
    with col_r:
        if st.button("REFRESH"):
            st.rerun()

    # Load all cached data
    god     = _load_json("god_signal.json")
    ll      = _load_json("lead_lag.json")
    health  = _load_json("system_health.json")

    # ── God-Signal Banner ─────────────────────────────────────────────────────
    halted = health.get("halted", False)
    fires  = god.get("fires", False)

    if halted:
        banner_cls = "banner-halt"
        banner_txt = "SYSTEM HALTED &mdash; circuit breaker or dead man's switch triggered"
    elif fires:
        banner_cls = "banner-fire"
        banner_txt = "&#x1F525; GOD-SIGNAL FIRES &mdash; all three gates passed"
    else:
        reason = god.get("reason", "waiting for signals...")
        banner_cls = "banner-hold"
        banner_txt = f"&#9208; HOLD &mdash; {reason}"

    st.markdown(f'<div class="signal-banner {banner_cls}">{banner_txt}</div>', unsafe_allow_html=True)
    st.markdown("---")

    # ── Row 1: Signal Gauges ──────────────────────────────────────────────────
    st.markdown('<div class="section-header">Signal Components</div>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)

    # Lead-Lag Correlation
    ll_score = god.get("lead_lag_score", 0.0)
    with c1:
        ll_color = "go" if ll_score > consensus_threshold else ("caution" if ll_score > 0.3 else "neutral")
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Lead-Lag Consensus</div>
            <div class="metric-value {ll_color}">{ll_score:.4f}</div>
            <div class="metric-sub">Pyth primary + Chainlink cross-check</div>
        </div>""", unsafe_allow_html=True)

        # Correlation matrix heatmap
        corr_matrix = ll.get("correlation_matrix", {})
        if corr_matrix:
            rows = []
            for leader, followers in corr_matrix.items():
                for follower, corr in followers.items():
                    rows.append({"Leader": leader, "Follower": follower, "r": corr})
            if rows:
                df = pd.DataFrame(rows)
                pivot = df.pivot(index="Leader", columns="Follower", values="r")
                _render_corr_matrix(pivot)

    # Bridge Z-Score
    bz = god.get("bridge_z", 0.0)
    with c2:
        bz_color = "go" if bz > bridge_threshold else ("caution" if bz > 0.0 else "neutral")
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Bridge Z-Score</div>
            <div class="metric-value {bz_color}">{bz:+.4f}</div>
            <div class="metric-sub">Threshold: &gt; {bridge_threshold:.2f} (dual-source validated)</div>
        </div>""", unsafe_allow_html=True)

    # Sentiment
    sent = god.get("sentiment_score", 0.0)
    with c3:
        s_color = "go" if sent > sentiment_threshold else ("caution" if sent > 0.0 else "neutral")
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Sentiment Score</div>
            <div class="metric-value {s_color}">{sent:+.4f}</div>
            <div class="metric-sub">Threshold: &gt; {sentiment_threshold:.2f} (range: -1 to +1)</div>
        </div>""", unsafe_allow_html=True)

    # ── Row 2: God-Signal Conviction Meter ────────────────────────────────────
    st.markdown('<div class="section-header">God-Signal Conviction</div>', unsafe_allow_html=True)

    consensus = god.get("consensus_score", 0.0)
    regime    = god.get("regime", "Unknown")
    hurst     = god.get("hurst", 0.0)

    if consensus > consensus_threshold:
        conv_color = "go"
        conv_label = "EXTREME CONVICTION"
    elif consensus > 0.6:
        conv_color = "caution"
        conv_label = "MODERATE"
    else:
        conv_color = "neutral"
        conv_label = "LOW"

    mc1, mc2 = st.columns([2, 3])
    with mc1:
        st.markdown(f"""
        <div class="metric-card conviction-meter">
            <div class="conviction-score {conv_color}">{consensus:.2f}</div>
            <div class="conviction-label">{conv_label}</div>
        </div>""", unsafe_allow_html=True)

    with mc2:
        sub_cols = st.columns(3)
        with sub_cols[0]:
            st.metric(
                "Lead-Lag",
                f"{ll_score:.3f}",
                delta="pass" if ll_score > consensus_threshold else "below threshold",
            )
        with sub_cols[1]:
            st.metric(
                "Bridge Z",
                f"{bz:+.3f}",
                delta="pass" if bz > bridge_threshold else "below threshold",
            )
        with sub_cols[2]:
            st.metric(
                "Sentiment",
                f"{sent:+.3f}",
                delta="pass" if sent > sentiment_threshold else "below threshold",
            )

        r_cols = st.columns(2)
        with r_cols[0]:
            st.metric("Regime", regime)
        with r_cols[1]:
            st.metric("Hurst", f"{hurst:.4f}")

    # ── Row 3: Shadow Mirror Portfolio ────────────────────────────────────────
    st.markdown('<div class="section-header">Shadow Mirror &mdash; Paper Trading</div>', unsafe_allow_html=True)

    portfolio = _load_json("shadow_portfolio.json") or _default_portfolio()
    equity_df = _load_equity_curve()

    pm1, pm2, pm3, pm4 = st.columns(4)
    total_val = portfolio.get("total_value", 0)
    cash_val  = portfolio.get("cash", 0)
    dd_pct    = portfolio.get("drawdown_pct", 0)
    sharpe    = portfolio.get("sharpe_rolling")

    with pm1:
        pnl_color = "go" if total_val >= cfg.PORTFOLIO_USD else "nogo"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Portfolio Value</div>
            <div class="metric-value {pnl_color}">${total_val:,.2f}</div>
            <div class="metric-sub">Starting: ${cfg.PORTFOLIO_USD:,.0f}</div>
        </div>""", unsafe_allow_html=True)

    with pm2:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Cash</div>
            <div class="metric-value">${cash_val:,.2f}</div>
            <div class="metric-sub">Available for deployment</div>
        </div>""", unsafe_allow_html=True)

    with pm3:
        dd_color = "nogo" if dd_pct < -10 else "neutral"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Drawdown</div>
            <div class="metric-value {dd_color}">{dd_pct:.1f}%</div>
            <div class="metric-sub">Limit: -15% / 24h</div>
        </div>""", unsafe_allow_html=True)

    with pm4:
        sharpe_str = f"{sharpe:.2f}" if sharpe is not None else "N/A"
        sh_color = "go" if sharpe and sharpe >= 1.0 else "neutral"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Rolling Sharpe</div>
            <div class="metric-value {sh_color}">{sharpe_str}</div>
            <div class="metric-sub">Target: &ge; 1.0</div>
        </div>""", unsafe_allow_html=True)

    # Equity curve chart
    if not equity_df.empty:
        st.line_chart(equity_df.set_index("timestamp")["total_value"], color="#00ff88")

    # Recent trades table
    trades_df = _load_shadow_trades()
    if not trades_df.empty:
        display_cols = [c for c in ["id", "timestamp", "signal_type", "asset", "side",
                                     "price", "net_cost", "status", "pnl_usd", "pnl_pct"]
                        if c in trades_df.columns]
        st.dataframe(trades_df[display_cols], width="stretch", hide_index=True)
    else:
        st.markdown(
            '<div class="metric-card neutral" style="text-align:center;padding:24px">'
            'No shadow trades yet. Start main_bot.py --passive to begin paper trading.'
            '</div>', unsafe_allow_html=True,
        )

    # ── Row 4: System Health ──────────────────────────────────────────────────
    st.markdown('<div class="section-header">System Health</div>', unsafe_allow_html=True)

    h1, h2, h3 = st.columns(3)

    # RPC Latencies
    with h1:
        heartbeats = health.get("heartbeats", {})
        hb_html = '<div class="metric-card"><div class="metric-label">RPC Endpoints</div>'
        if heartbeats:
            for name, hb in heartbeats.items():
                alive = hb.get("alive", False)
                lat   = hb.get("latency_ms", 0)
                fails = hb.get("consecutive_failures", 0)
                cls   = "hb-ok" if alive else ("hb-warn" if fails < 3 else "hb-fail")
                icon  = "OK" if alive else f"FAIL({fails})"
                hb_html += (
                    f'<div style="margin:6px 0;font-family:JetBrains Mono,monospace;font-size:0.75rem">'
                    f'<span class="{cls}">[{icon}]</span> {name[:35]}: {lat:.0f}ms</div>'
                )
        else:
            hb_html += '<div class="metric-sub">No heartbeat data</div>'
        hb_html += '</div>'
        st.markdown(hb_html, unsafe_allow_html=True)

    # Flash-Crash
    with h2:
        fc = health.get("flash_crash", {})
        fc_halted = fc.get("halted", False)
        fc_dev    = fc.get("deviation_pct", 0)
        fc_color  = "nogo" if fc_halted else "go"
        fc_status = "HALTED" if fc_halted else "CLEAR"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Flash-Crash Breaker</div>
            <div class="metric-value {fc_color}">{fc_status}</div>
            <div class="metric-sub">Deviation: {fc_dev:.2f}% (limit: 3%)</div>
        </div>""", unsafe_allow_html=True)

    # Circuit Breaker
    with h3:
        cb_clear = health.get("circuit_breaker_clear", True)
        cb_triggered = health.get("circuit_breaker_triggered", [])
        cb_color  = "go" if cb_clear else "nogo"
        cb_status = "CLEAR" if cb_clear else "TRIGGERED"
        cb_detail = "All checks passed" if cb_clear else ", ".join(cb_triggered)
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Circuit Breaker</div>
            <div class="metric-value {cb_color}">{cb_status}</div>
            <div class="metric-sub">{cb_detail}</div>
        </div>""", unsafe_allow_html=True)

    # Footer
    ts = health.get("timestamp", god.get("timestamp", "—"))
    st.markdown(f'<p style="text-align:center;font-size:0.6rem;color:#333;margin-top:40px">'
                f'Last update: {ts}</p>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()

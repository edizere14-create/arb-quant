"""
Daily Signal Monitor — Bridge Inflow Momentum Watcher
Runs bridge_signal.py logic daily, logs to signal_log.csv.

Usage (manual):
    python monitor.py

Windows Task Scheduler setup:
    Action:  Start a program
    Program: python
    Args:    "C:\\Users\\eddyi\\arb-quant\\monitor.py"
    Start in: C:\\Users\\eddyi\\arb-quant
    Trigger: Daily, e.g. 08:00 UTC
"""

import csv
import os
import sys
from datetime import datetime, timezone

# Ensure imports resolve from the script's own directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import numpy as np
from bridge_signal import (
    fetch_bridge_inflows,
    fetch_arb_prices,
    bridge_signal,
    inflow_signal_is_valid,
    Z_LOOKBACK,
    Z_ENTRY,
    CORR_LOOKBACK,
)

LOG_FILE = os.path.join(SCRIPT_DIR, "signal_log.csv")
LOG_FIELDS = [
    "timestamp",
    "z_score",
    "pearson_r",
    "p_value",
    "corr_valid",
    "entry_signal",
    "decision",
]


def _ensure_log_header() -> None:
    """Create signal_log.csv with header if it doesn't exist."""
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()


def _append_log(row: dict) -> None:
    """Append one row to signal_log.csv."""
    _ensure_log_header()
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writerow(row)


def run_check() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[monitor] {now}")

    # ── Fetch data ───────────────────────────────────────────────────────
    try:
        inflows, source = fetch_bridge_inflows()
        inflows = inflows.dropna()
    except Exception as e:
        print(f"  ❌ Failed to fetch bridge inflows: {e}")
        _append_log({
            "timestamp": now, "z_score": "", "pearson_r": "", "p_value": "",
            "corr_valid": "", "entry_signal": "", "decision": f"ERROR: {e}",
        })
        return

    # ── Z-score ──────────────────────────────────────────────────────────
    z_scores = bridge_signal(inflows, lookback=Z_LOOKBACK)
    current_z = z_scores.iloc[-1]
    if np.isnan(current_z):
        current_z = 0.0
    entry_signal = current_z > Z_ENTRY

    # ── Correlation check ────────────────────────────────────────────────
    try:
        arb_prices = fetch_arb_prices()
        arb_fwd_return = arb_prices.pct_change().shift(-1)
        common = z_scores.dropna().index.intersection(arb_fwd_return.dropna().index)
        if len(common) < 30:
            sig_valid, corr_r, corr_p = False, 0.0, 1.0
        else:
            z_aligned = z_scores.reindex(common)
            ret_aligned = arb_fwd_return.reindex(common)
            sig_valid, corr_r, corr_p = inflow_signal_is_valid(
                z_aligned, ret_aligned, lookback=CORR_LOOKBACK
            )
    except Exception as e:
        print(f"  ⚠️  Correlation check failed: {e}")
        sig_valid, corr_r, corr_p = False, 0.0, 1.0

    # ── Decision ─────────────────────────────────────────────────────────
    if sig_valid and entry_signal:
        decision = "ENTRY_SIGNAL"
        print(f"  ⚡ ENTRY SIGNAL — correlation restored (r={corr_r:+.4f}, "
              f"p={corr_p:.4f}), z={current_z:+.4f}")
        print("     → Run bridge_signal.py for full fee drag check")
    else:
        decision = "PARKED"
        reasons = []
        if not entry_signal:
            reasons.append(f"z={current_z:+.4f} < {Z_ENTRY}")
        if not sig_valid:
            reasons.append(f"corr decayed (r={corr_r:+.4f}, p={corr_p:.4f})")
        print(f"  ⏸ PARKED — recheck tomorrow  [{'; '.join(reasons)}]")

    # ── Log ───────────────────────────────────────────────────────────────
    _append_log({
        "timestamp": now,
        "z_score": f"{current_z:+.4f}",
        "pearson_r": f"{corr_r:+.4f}",
        "p_value": f"{corr_p:.4f}",
        "corr_valid": str(sig_valid),
        "entry_signal": str(entry_signal),
        "decision": decision,
    })
    print(f"  📝 Logged to {LOG_FILE}")


if __name__ == "__main__":
    run_check()

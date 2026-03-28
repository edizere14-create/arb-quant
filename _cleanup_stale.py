"""One-shot cleanup: close all stale phantom trades (price=1) and reset portfolio."""
import sqlite3
from datetime import datetime, timezone

db = sqlite3.connect("shadow_trades.db")
db.row_factory = sqlite3.Row

now = datetime.now(timezone.utc).isoformat()

# 1. Count stale trades
stale = db.execute("SELECT COUNT(*) as cnt FROM trades WHERE status = 'OPEN' AND price = 1").fetchone()["cnt"]
print(f"Stale OPEN trades with price=1: {stale}")

# 2. Close them at entry price (only gas+slippage drag)
db.execute("""
    UPDATE trades
    SET status = 'CLOSED',
        exit_price = price,
        exit_timestamp = ?,
        pnl_usd = -(simulated_gas_fee + (price * quantity * simulated_slippage_pct)),
        pnl_pct = ROUND(-((simulated_gas_fee + (price * quantity * simulated_slippage_pct)) / net_cost) * 100, 4),
        notes = COALESCE(notes, '') || ' | BULK_CLOSED: stale pre-fix phantom trade'
    WHERE status = 'OPEN' AND price = 1
""", (now,))
affected = db.total_changes
print(f"Closed {stale} stale trades")

# 3. Reset position_state for ARB
db.execute("""
    UPDATE position_state
    SET active_position = 0, cooldown_until = 0, last_trade_side = 'SELL', updated_at = ?
    WHERE asset = 'ARB'
""", (now,))

# 4. Clear stale portfolio snapshots
db.execute("DELETE FROM portfolio_snapshots")
print("Cleared portfolio snapshots")

db.commit()

# 5. Verify
remaining = db.execute("SELECT COUNT(*) as cnt FROM trades WHERE status = 'OPEN'").fetchone()["cnt"]
closed = db.execute("SELECT COUNT(*) as cnt FROM trades WHERE status = 'CLOSED'").fetchone()["cnt"]
total_pnl = db.execute("SELECT COALESCE(SUM(pnl_usd), 0) as pnl FROM trades WHERE status = 'CLOSED'").fetchone()["pnl"]

print(f"\n--- Post-cleanup ---")
print(f"Remaining OPEN: {remaining}")
print(f"Total CLOSED: {closed}")
print(f"Total realized PnL: ${total_pnl:.2f}")
print(f"Portfolio: ${1000 + total_pnl:.2f}")

db.close()

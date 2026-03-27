"""
shadow_mirror.py  (v6.0 — God-Tier Edition)
SQLite-backed paper trading module.
Simulates gas fees ($0.02) and slippage (0.1%) for every signal.
No real ETH is spent — all trades are virtual.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import aiosqlite
import numpy as np

import config as cfg

logger = logging.getLogger("shadow_mirror")

RowDict = dict[str, Any]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT    NOT NULL,
    signal_type           TEXT    NOT NULL,
    asset                 TEXT    NOT NULL,
    side                  TEXT    NOT NULL,
    price                 REAL    NOT NULL,
    quantity              REAL    NOT NULL,
    simulated_gas_fee     REAL    DEFAULT 0.02,
    simulated_slippage_pct REAL   DEFAULT 0.001,
    net_cost              REAL    NOT NULL,
    status                TEXT    DEFAULT 'OPEN',
    exit_price            REAL,
    exit_timestamp        TEXT,
    pnl_usd               REAL,
    pnl_pct               REAL,
    god_signal_score      REAL,
    bridge_z              REAL,
    sentiment             REAL,
    notes                 TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    total_value      REAL    NOT NULL,
    cash             REAL    NOT NULL,
    positions_value  REAL    NOT NULL,
    drawdown_pct     REAL,
    sharpe_rolling   REAL
);

CREATE TABLE IF NOT EXISTS position_state (
    asset            TEXT    PRIMARY KEY,
    active_position  INTEGER NOT NULL DEFAULT 0,
    cooldown_until   REAL    NOT NULL DEFAULT 0,
    last_trade_side  TEXT,
    last_trade_at    TEXT,
    updated_at       TEXT    NOT NULL
);
"""


class ShadowMirror:
    """Async SQLite paper trading engine."""

    def __init__(self, db_path: str = cfg.SHADOW_DB_PATH, starting_capital: float = cfg.PORTFOLIO_USD):
        self.db_path = db_path
        self.starting_capital = starting_capital
        self._db: aiosqlite.Connection | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.executescript(_SCHEMA)
            await self._db.commit()
        return self._db

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _infer_active_position(self, asset: str) -> bool:
        db = await self._get_db()
        rows = cast(list[aiosqlite.Row], await db.execute_fetchall(
            "SELECT COUNT(*) AS open_count FROM trades WHERE asset = ? AND status = 'OPEN'",
            (asset,),
        ))
        open_count = int(rows[0]["open_count"]) if rows else 0
        return open_count > 0

    async def get_position_state(self, asset: str) -> RowDict:
        asset_key = asset.strip().upper()
        db = await self._get_db()
        rows = cast(list[aiosqlite.Row], await db.execute_fetchall(
            "SELECT * FROM position_state WHERE asset = ?",
            (asset_key,),
        ))
        inferred_active = await self._infer_active_position(asset_key)

        if rows:
            state = dict(rows[0])
            state["active_position"] = bool(state.get("active_position", 0) or inferred_active)
            if state["active_position"] != bool(rows[0]["active_position"]):
                await self.upsert_position_state(
                    asset=asset_key,
                    active_position=state["active_position"],
                    cooldown_until=float(state.get("cooldown_until", 0.0) or 0.0),
                    last_trade_side=state.get("last_trade_side"),
                    last_trade_at=state.get("last_trade_at"),
                )
            return state

        state = {
            "asset": asset_key,
            "active_position": inferred_active,
            "cooldown_until": 0.0,
            "last_trade_side": None,
            "last_trade_at": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.upsert_position_state(
            asset=asset_key,
            active_position=inferred_active,
            cooldown_until=0.0,
            last_trade_side=None,
            last_trade_at=None,
        )
        return state

    async def upsert_position_state(
        self,
        asset: str,
        active_position: bool,
        cooldown_until: float,
        last_trade_side: str | None,
        last_trade_at: str | None,
    ) -> RowDict:
        asset_key = asset.strip().upper()
        db = await self._get_db()
        updated_at = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """
            INSERT INTO position_state
                (asset, active_position, cooldown_until, last_trade_side, last_trade_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset) DO UPDATE SET
                active_position = excluded.active_position,
                cooldown_until = excluded.cooldown_until,
                last_trade_side = excluded.last_trade_side,
                last_trade_at = excluded.last_trade_at,
                updated_at = excluded.updated_at
            """,
            (
                asset_key,
                int(active_position),
                float(max(0.0, cooldown_until)),
                last_trade_side,
                last_trade_at,
                updated_at,
            ),
        )
        await db.commit()
        return {
            "asset": asset_key,
            "active_position": bool(active_position),
            "cooldown_until": float(max(0.0, cooldown_until)),
            "last_trade_side": last_trade_side,
            "last_trade_at": last_trade_at,
            "updated_at": updated_at,
        }

    async def mark_trade_state(
        self,
        asset: str,
        side: str,
        cooldown_seconds: int = 300,
    ) -> RowDict:
        side_key = side.strip().upper()
        now = datetime.now(timezone.utc).isoformat()
        cooldown_until = time.time() + max(0, cooldown_seconds)
        active_position = side_key != "SELL"
        return await self.upsert_position_state(
            asset=asset,
            active_position=active_position,
            cooldown_until=cooldown_until,
            last_trade_side=side_key,
            last_trade_at=now,
        )

    # ── Record a trade ────────────────────────────────────────────────────────

    async def record_trade(
        self,
        signal_type: str,
        asset: str,
        side: str,
        price: float,
        quantity: float,
        god_signal_score: float = 0.0,
        bridge_z: float = 0.0,
        sentiment: float = 0.0,
        notes: str = "",
    ) -> int:
        """
        Insert a new paper trade with simulated gas + slippage.
        Returns the trade ID.
        """
        gas = cfg.SIMULATED_GAS_FEE_USD
        slip_pct = cfg.SIMULATED_SLIPPAGE_PCT

        gross_cost = price * quantity
        slippage_cost = gross_cost * slip_pct
        if side == "BUY":
            net_cost = gross_cost + slippage_cost + gas
        else:
            net_cost = gross_cost - slippage_cost - gas

        ts = datetime.now(timezone.utc).isoformat()
        db = await self._get_db()
        cursor = await db.execute(
            """
            INSERT INTO trades
                (timestamp, signal_type, asset, side, price, quantity,
                 simulated_gas_fee, simulated_slippage_pct, net_cost,
                 status, god_signal_score, bridge_z, sentiment, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
            """,
            (ts, signal_type, asset, side, price, quantity,
             gas, slip_pct, round(net_cost, 4),
             god_signal_score, bridge_z, sentiment, notes),
        )
        await db.commit()
        trade_id = int(cursor.lastrowid or 0)
        await self.mark_trade_state(asset=asset, side=side)
        logger.info(
            "[SHADOW] %s %s %.6f %s @ $%.4f | net $%.4f | id=%d",
            side, asset, quantity, signal_type, price, net_cost, trade_id,
        )
        return trade_id

    # ── Close a trade ─────────────────────────────────────────────────────────

    async def close_trade(self, trade_id: int, exit_price: float) -> RowDict:
        """Close an open paper trade and compute PnL."""
        db = await self._get_db()
        row = cast(list[aiosqlite.Row], await db.execute_fetchall(
            "SELECT * FROM trades WHERE id = ? AND status = 'OPEN'", (trade_id,)
        ))
        if not row:
            return {"error": f"Trade {trade_id} not found or already closed"}

        trade = dict(row[0])
        entry_price = trade["price"]
        quantity = trade["quantity"]
        side = trade["side"]

        exit_slip = exit_price * quantity * cfg.SIMULATED_SLIPPAGE_PCT
        exit_gas = cfg.SIMULATED_GAS_FEE_USD

        if side == "BUY":
            pnl_usd = (exit_price - entry_price) * quantity - exit_slip - exit_gas
        else:
            pnl_usd = (entry_price - exit_price) * quantity - exit_slip - exit_gas

        pnl_pct = (pnl_usd / trade["net_cost"] * 100) if trade["net_cost"] != 0 else 0.0
        exit_ts = datetime.now(timezone.utc).isoformat()

        await db.execute(
            """
            UPDATE trades
            SET status = 'CLOSED', exit_price = ?, exit_timestamp = ?,
                pnl_usd = ?, pnl_pct = ?
            WHERE id = ?
            """,
            (exit_price, exit_ts, round(pnl_usd, 4), round(pnl_pct, 4), trade_id),
        )
        await db.commit()
        await self.upsert_position_state(
            asset=trade["asset"],
            active_position=False,
            cooldown_until=time.time() + 300,
            last_trade_side="SELL",
            last_trade_at=exit_ts,
        )

        logger.info(
            "[SHADOW] CLOSED trade %d: PnL $%.4f (%.2f%%)", trade_id, pnl_usd, pnl_pct,
        )
        return {
            "trade_id": trade_id,
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_pct, 4),
            "exit_price": exit_price,
            "exit_timestamp": exit_ts,
        }

    # ── Portfolio snapshot ────────────────────────────────────────────────────

    async def get_portfolio_snapshot(self) -> RowDict:
        """Current portfolio state: cash + open positions."""
        db = await self._get_db()

        # Sum closed P&L
        rows = cast(list[aiosqlite.Row], await db.execute_fetchall(
            "SELECT COALESCE(SUM(pnl_usd), 0) as total_pnl FROM trades WHERE status = 'CLOSED'"
        ))
        total_pnl = rows[0]["total_pnl"] if rows else 0.0

        # Open positions cost
        open_rows = cast(list[aiosqlite.Row], await db.execute_fetchall(
            "SELECT COALESCE(SUM(net_cost), 0) as open_cost FROM trades WHERE status = 'OPEN'"
        ))
        open_cost = open_rows[0]["open_cost"] if open_rows else 0.0

        cash = self.starting_capital + total_pnl - open_cost
        total_value = cash + open_cost
        drawdown_pct = ((total_value - self.starting_capital) / self.starting_capital * 100
                        if total_value < self.starting_capital else 0.0)

        sharpe = await self.compute_rolling_sharpe()

        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_value": round(total_value, 2),
            "cash": round(cash, 2),
            "positions_value": round(open_cost, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "sharpe_rolling": sharpe,
        }

        # Persist snapshot
        await db.execute(
            """
            INSERT INTO portfolio_snapshots
                (timestamp, total_value, cash, positions_value, drawdown_pct, sharpe_rolling)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (snapshot["timestamp"], snapshot["total_value"], snapshot["cash"],
             snapshot["positions_value"], snapshot["drawdown_pct"], sharpe),
        )
        await db.commit()

        # Cache for HUD
        (cfg.CACHE_DIR / "shadow_portfolio.json").write_text(
            json.dumps(snapshot, indent=2, default=str)
        )
        return snapshot

    # ── Trade history ─────────────────────────────────────────────────────────

    async def get_trade_history(self, limit: int = 50) -> list[RowDict]:
        db = await self._get_db()
        rows = cast(list[aiosqlite.Row], await db.execute_fetchall(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ))
        return [dict(r) for r in rows]

    async def get_open_trades(self) -> list[RowDict]:
        db = await self._get_db()
        rows = cast(list[aiosqlite.Row], await db.execute_fetchall(
            "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id DESC"
        ))
        return [dict(r) for r in rows]

    # ── Rolling Sharpe ────────────────────────────────────────────────────────

    async def compute_rolling_sharpe(self, window: int = 30) -> float | None:
        """Compute rolling Sharpe ratio from closed trade returns."""
        db = await self._get_db()
        rows = cast(list[aiosqlite.Row], await db.execute_fetchall(
            "SELECT pnl_pct FROM trades WHERE status = 'CLOSED' ORDER BY id ASC"
        ))
        if len(rows) < 5:
            return None

        returns = np.array([r["pnl_pct"] / 100.0 for r in rows])
        recent = returns[-window:] if len(returns) >= window else returns
        mean_r = float(np.mean(recent))
        std_r = float(np.std(recent, ddof=1))
        if std_r < 1e-9:
            return None

        # Annualize assuming ~1 trade/day
        sharpe = (mean_r / std_r) * np.sqrt(365)
        return float(round(sharpe, 4))

    # ── Equity curve data ─────────────────────────────────────────────────────

    async def get_equity_curve(self) -> list[RowDict]:
        db = await self._get_db()
        rows = cast(list[aiosqlite.Row], await db.execute_fetchall(
            "SELECT timestamp, total_value, drawdown_pct, sharpe_rolling "
            "FROM portfolio_snapshots ORDER BY id ASC"
        ))
        return [dict(r) for r in rows]


# ── Standalone test ───────────────────────────────────────────────────────────

async def _test_shadow() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    sm = ShadowMirror(db_path=":memory:", starting_capital=1000.0)
    try:
        print(f"\n{'='*60}")
        print("  SHADOW MIRROR v6.0 — Paper Trading Test")
        print(f"{'='*60}")

        print("\n[1/4] Record BUY trade...")
        tid = await sm.record_trade(
            signal_type="GOD_SIGNAL", asset="ARB", side="BUY",
            price=1.25, quantity=100.0, god_signal_score=0.9,
            bridge_z=2.1, sentiment=0.55,
        )
        print(f"      Trade ID: {tid}")

        print("\n[2/4] Portfolio snapshot...")
        snap = await sm.get_portfolio_snapshot()
        print(f"      Total: ${snap['total_value']:.2f}  Cash: ${snap['cash']:.2f}")

        print("\n[3/4] Close trade at profit...")
        result = await sm.close_trade(tid, exit_price=1.35)
        print(f"      PnL: ${result['pnl_usd']:.4f} ({result['pnl_pct']:.2f}%)")

        print("\n[4/4] Final portfolio...")
        snap2 = await sm.get_portfolio_snapshot()
        print(f"      Total: ${snap2['total_value']:.2f}  Sharpe: {snap2['sharpe_rolling']}")

        history = await sm.get_trade_history()
        print(f"\n      Trade history: {len(history)} trades")
        print(f"{'='*60}\n")

    finally:
        await sm.close()


if __name__ == "__main__":
    asyncio.run(_test_shadow())

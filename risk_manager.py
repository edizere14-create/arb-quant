"""
risk_manager.py  (v6.0 — God-Tier Edition)
Async risk management: Half-Kelly sizing, Flash-Crash circuit breaker,
and Dead Man's Switch heartbeat monitor.

Wraps existing modules:
  - position_sizer.compute_position_size()
  - circuit_breaker.run_circuit_breaker()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
import numpy as np

from position_sizer import compute_position_size
from circuit_breaker import run_circuit_breaker

import config as cfg

logger = logging.getLogger("risk_manager")


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class ActivePosition:
    """Tracks a live position with hard stop, breakeven ratchet, and take-profit."""
    asset: str
    trade_id: int
    entry_price: float
    position_size: float           # USD notional
    highest_price_reached: float
    hard_stop_price: float         # entry * 0.985 — immutable floor
    trailing_stop_price: float     # starts at hard_stop, ratchets up
    take_profit_price: float       # entry * 1.03
    status: str = "OPEN"           # OPEN | CLOSED

    # ── Breakeven ratchet ─────────────────────────────────────────────────
    def update_trailing(self, current_price: float) -> None:
        """Call every tick.  Raises trailing stop once price exceeds +1%."""
        if current_price <= 0 or self.status != "OPEN":
            return
        if current_price > self.highest_price_reached:
            self.highest_price_reached = current_price
        # Breakeven ratchet: once +1% above entry, lock stop at entry + 0.1%
        breakeven_threshold = self.entry_price * 1.01
        breakeven_stop = self.entry_price * 1.001
        if self.highest_price_reached >= breakeven_threshold:
            self.trailing_stop_price = max(self.trailing_stop_price, breakeven_stop)

    def check_exit(self, current_price: float) -> tuple[bool, str]:
        """Returns (should_exit, reason).  Checks stop and take-profit."""
        if self.status != "OPEN":
            return False, ""
        if current_price <= self.trailing_stop_price:
            reason = "HARD_STOP" if self.trailing_stop_price <= self.hard_stop_price else "TRAILING_STOP"
            return True, reason
        if current_price >= self.take_profit_price:
            return True, "TAKE_PROFIT"
        return False, ""

    @staticmethod
    def from_trade(asset: str, trade_id: int, entry_price: float, position_size: float) -> "ActivePosition":
        hard_stop = entry_price * 0.985
        return ActivePosition(
            asset=asset,
            trade_id=trade_id,
            entry_price=entry_price,
            position_size=position_size,
            highest_price_reached=entry_price,
            hard_stop_price=hard_stop,
            trailing_stop_price=hard_stop,
            take_profit_price=entry_price * 1.03,
        )


@dataclass
class PositionSizeResult:
    position_usd: float
    pct_of_portfolio: float
    signal_score: int
    tradeable: bool
    kelly_fraction: float
    vol_adjustment: float
    strategy_mode: str
    risk_scaler: float
    max_trade_pct: float
    raw: dict[str, object]


@dataclass
class FlashCrashState:
    halted: bool
    current_price: float
    ema_price: float
    deviation_pct: float
    threshold_pct: float
    timestamp: str


@dataclass
class HeartbeatStatus:
    service: str
    alive: bool
    latency_ms: float
    consecutive_failures: int
    last_success: float


@dataclass
class SystemHealth:
    all_healthy: bool
    halted: bool
    heartbeats: dict[str, HeartbeatStatus]
    flash_crash: FlashCrashState
    circuit_breaker_clear: bool
    circuit_breaker_triggered: list[str]
    timestamp: str


# ── Risk Manager ──────────────────────────────────────────────────────────────

class RiskManager:
    """
    Async risk management combining:
      A. Half-Kelly position sizing (wraps position_sizer.py)
      B. Flash-Crash circuit breaker (3% deviation from 5-min EMA)
      C. Dead Man's Switch (heartbeat monitor for RPC/API services)
    """

    def __init__(
        self,
        portfolio_usd: float = cfg.PORTFOLIO_USD,
        halt_event: asyncio.Event | None = None,
    ):
        self.portfolio_usd = portfolio_usd
        self.mode = cfg.STRATEGY_MODE if cfg.STRATEGY_MODE in {"SNIPER", "MULTI_ASSET"} else "SNIPER"
        self.active_positions: dict[str, bool] = {}
        self.cooldowns: dict[str, float] = {}
        self.positions: dict[str, ActivePosition] = {}  # asset → live position

        # Event that other loops watch — set = halt everything
        self.halt_event = halt_event or asyncio.Event()

        # Flash-crash EMA state
        self._price_buffer: deque[tuple[float, float]] = deque(maxlen=600)  # 5min @ 0.5s
        self._ema_price: float = 0.0
        self._ema_alpha: float = 2.0 / (cfg.EMA_WINDOW_SECONDS + 1)

        # Heartbeat tracking
        self._heartbeats: dict[str, HeartbeatStatus] = {}
        self._http_client: httpx.AsyncClient | None = None

    @staticmethod
    def _rpc_service_name(url: str) -> str:
        host = urlparse(url).netloc or url
        return f"rpc:{host}"

    @staticmethod
    def _portfolio_growth_progress(portfolio_usd: float) -> float:
        base = min(cfg.PORTFOLIO_GROWTH_BASE_USD, cfg.PORTFOLIO_GROWTH_TARGET_USD)
        target = max(cfg.PORTFOLIO_GROWTH_BASE_USD, cfg.PORTFOLIO_GROWTH_TARGET_USD)
        if target <= base:
            return 1.0
        progress = (portfolio_usd - base) / (target - base)
        return max(0.0, min(1.0, progress))

    def update_portfolio(self, portfolio_usd: float) -> None:
        self.portfolio_usd = max(0.0, portfolio_usd)

    @staticmethod
    def _normalize_asset(asset: str) -> str:
        return asset.strip().upper()

    @staticmethod
    def _parse_float(value: object, default: float = 0.0) -> float:
        candidate = value if isinstance(value, (int, float, str)) else default
        try:
            return float(candidate)
        except (TypeError, ValueError):
            return default

    def sync_asset_state(self, asset: str, state: dict[str, object] | None) -> dict[str, object]:
        asset_key = self._normalize_asset(asset)
        state = state or {}
        active_position = bool(state.get("active_position", False))
        cooldown_until = max(0.0, self._parse_float(state.get("cooldown_until", 0.0)))
        self.active_positions[asset_key] = active_position
        self.cooldowns[asset_key] = cooldown_until
        return {
            "asset": asset_key,
            "active_position": active_position,
            "cooldown_until": cooldown_until,
        }

    def cooldown_remaining(self, asset: str, now: float | None = None) -> int:
        asset_key = self._normalize_asset(asset)
        current_time = time.time() if now is None else now
        cooldown_until = self.cooldowns.get(asset_key, 0.0)
        if cooldown_until <= current_time:
            self.cooldowns[asset_key] = 0.0
            return 0
        return int(np.ceil(cooldown_until - current_time))

    def is_trade_locked(self, asset: str, side: str, now: float | None = None) -> tuple[bool, str]:
        asset_key = self._normalize_asset(asset)
        side_key = side.strip().upper()
        remaining = self.cooldown_remaining(asset_key, now=now)
        if remaining > 0:
            return True, f"COOLDOWN_ACTIVE ({remaining}s remaining)"
        if side_key == "BUY" and self.mode == "SNIPER" and self.active_positions.get(asset_key, False):
            return True, "SNIPER_POSITION_LOCK"
        return False, ""

    def register_trade(self, asset: str, side: str, cooldown_seconds: int = 300, now: float | None = None) -> None:
        asset_key = self._normalize_asset(asset)
        side_key = side.strip().upper()
        current_time = time.time() if now is None else now
        self.cooldowns[asset_key] = current_time + max(0, cooldown_seconds)
        self.active_positions[asset_key] = side_key != "SELL"

    # ── ActivePosition lifecycle ──────────────────────────────────────────

    def open_position(self, asset: str, trade_id: int, entry_price: float, position_size: float) -> ActivePosition:
        """Create and store an ActivePosition after a BUY is recorded."""
        asset_key = self._normalize_asset(asset)
        pos = ActivePosition.from_trade(asset_key, trade_id, entry_price, position_size)
        self.positions[asset_key] = pos
        logger.info(
            "Position OPENED: %s trade#%d entry=$%.4f stop=$%.4f tp=$%.4f",
            asset_key, trade_id, entry_price, pos.hard_stop_price, pos.take_profit_price,
        )
        return pos

    def close_position(self, asset: str) -> ActivePosition | None:
        """Remove the ActivePosition for an asset.  Returns the closed position or None."""
        asset_key = self._normalize_asset(asset)
        pos = self.positions.pop(asset_key, None)
        if pos:
            pos.status = "CLOSED"
        return pos

    def get_position(self, asset: str) -> ActivePosition | None:
        return self.positions.get(self._normalize_asset(asset))

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                headers={"User-Agent": "arb-quant/6.0"},
                follow_redirects=True,
            )
        return self._http_client

    async def close(self) -> None:
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    # ── A. Half-Kelly Position Sizing ─────────────────────────────────────────

    async def compute_position(
        self,
        signal_score: int,
        rv_override: float | None = None,
        active_positions: int = 0,
        portfolio_usd: float | None = None,
    ) -> PositionSizeResult:
        """
        Wrap position_sizer.compute_position_size() via asyncio.to_thread.
        Uses updated portfolio from config (default $1,000, scales to $5,000).
        """
        current_portfolio = max(0.0, portfolio_usd if portfolio_usd is not None else self.portfolio_usd)
        raw = await asyncio.to_thread(
            compute_position_size,
            signal_score=signal_score,
            portfolio_usd=current_portfolio,
            rv_override=rv_override,
            verbose=False,
        )
        raw_tradeable = bool(raw.get("tradeable", False))
        raw_fraction = self._parse_float(raw.get("raw_fraction", 0.0))
        vol_adjustment = self._parse_float(raw.get("vol_adjustment", 0.0))

        strategy_mode = self.mode
        growth_progress = self._portfolio_growth_progress(current_portfolio)
        risk_scaler = 1.0 - growth_progress * (1.0 - cfg.GROWTH_RISK_FLOOR)
        mode_trade_cap = (
            cfg.SNIPER_MAX_TRADE_PCT if strategy_mode == "SNIPER"
            else cfg.MULTI_ASSET_MAX_TRADE_PCT
        )
        scaled_fraction = raw_fraction * risk_scaler
        capped_fraction = min(scaled_fraction, mode_trade_cap, cfg.MAX_STRATEGY_PCT)
        position_usd = round(current_portfolio * capped_fraction, 2) if raw_tradeable else 0.0
        if raw_tradeable and position_usd < cfg.MIN_POSITION_USD:
            position_usd = cfg.MIN_POSITION_USD
        max_trade_pct = min(mode_trade_cap, cfg.MAX_STRATEGY_PCT)
        tradeable = raw_tradeable and position_usd >= cfg.MIN_POSITION_USD

        if strategy_mode == "MULTI_ASSET" and active_positions >= cfg.MULTI_ASSET_MAX_OPEN_POSITIONS:
            tradeable = False
            position_usd = 0.0

        pct_of_portfolio = (position_usd / current_portfolio * 100) if current_portfolio > 0 else 0.0
        sizing_raw = {
            **raw,
            "portfolio_usd": round(current_portfolio, 2),
            "strategy_mode": strategy_mode,
            "growth_progress": round(growth_progress, 4),
            "risk_scaler": round(risk_scaler, 4),
            "mode_trade_cap_pct": round(max_trade_pct, 4),
            "active_positions": active_positions,
            "position_usd": position_usd,
            "pct_of_portfolio": round(pct_of_portfolio, 1),
            "tradeable": tradeable,
        }
        return PositionSizeResult(
            position_usd=position_usd,
            pct_of_portfolio=round(pct_of_portfolio, 1),
            signal_score=signal_score,
            tradeable=tradeable,
            kelly_fraction=capped_fraction,
            vol_adjustment=vol_adjustment,
            strategy_mode=strategy_mode,
            risk_scaler=round(risk_scaler, 4),
            max_trade_pct=max_trade_pct,
            raw=sizing_raw,
        )

    # ── B. Flash-Crash Circuit Breaker ────────────────────────────────────────

    def update_price(self, price: float) -> FlashCrashState:
        """
        Feed a new price tick. Updates EMA and checks 3% deviation.
        Call this every time a price update arrives.
        """
        now = time.time()
        self._price_buffer.append((now, price))

        if self._ema_price == 0.0:
            self._ema_price = price
        else:
            self._ema_price = self._ema_alpha * price + (1 - self._ema_alpha) * self._ema_price

        deviation = abs(price - self._ema_price) / self._ema_price if self._ema_price > 0 else 0.0
        halted = deviation > cfg.FLASH_CRASH_DEVIATION

        if halted:
            logger.warning(
                "FLASH-CRASH HALT: price=%.4f ema=%.4f dev=%.4f%% > %.4f%%",
                price, self._ema_price, deviation * 100, cfg.FLASH_CRASH_DEVIATION * 100,
            )
            self.halt_event.set()

        state = FlashCrashState(
            halted=halted,
            current_price=round(price, 6),
            ema_price=round(self._ema_price, 6),
            deviation_pct=round(deviation * 100, 4),
            threshold_pct=round(cfg.FLASH_CRASH_DEVIATION * 100, 2),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        return state

    async def check_flash_crash(self) -> FlashCrashState:
        """Fetch current price and run flash-crash check."""
        client = await self._get_client()
        try:
            resp = await client.get(
                "https://coins.llama.fi/prices/current/coingecko:ethereum",
                timeout=8.0,
            )
            resp.raise_for_status()
            data = resp.json()
            price = float(data["coins"]["coingecko:ethereum"]["price"])
            return self.update_price(price)
        except Exception as exc:
            logger.warning("Flash-crash price fetch failed: %s", exc)
            return FlashCrashState(
                halted=False, current_price=0.0, ema_price=self._ema_price,
                deviation_pct=0.0, threshold_pct=cfg.FLASH_CRASH_DEVIATION * 100,
                timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            )

    # ── C. Dead Man's Switch ──────────────────────────────────────────────────

    async def _ping_rpc(self, url: str) -> tuple[bool, float]:
        """Ping an RPC endpoint, return (alive, latency_ms)."""
        client = await self._get_client()
        start = time.monotonic()
        try:
            resp = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                timeout=5.0,
            )
            elapsed = (time.monotonic() - start) * 1000
            data = resp.json()
            alive = "result" in data
            return alive, round(elapsed, 1)
        except Exception:
            elapsed = (time.monotonic() - start) * 1000
            return False, round(elapsed, 1)

    async def _ping_api(self, url: str) -> tuple[bool, float]:
        """Ping a REST API endpoint."""
        client = await self._get_client()
        start = time.monotonic()
        try:
            resp = await client.get(url, timeout=5.0)
            elapsed = (time.monotonic() - start) * 1000
            return resp.status_code < 500, round(elapsed, 1)
        except Exception:
            elapsed = (time.monotonic() - start) * 1000
            return False, round(elapsed, 1)

    async def _ping_with_retry(
        self,
        name: str,
        ping_fn: Callable[..., Awaitable[tuple[bool, float]]],
        *args: str,
    ) -> HeartbeatStatus:
        """Ping with exponential backoff retries."""
        prev = self._heartbeats.get(name)
        prev_fails = prev.consecutive_failures if prev else 0

        for attempt, delay in enumerate(cfg.RETRY_DELAYS_S):
            alive, latency = await ping_fn(*args)
            if alive:
                status = HeartbeatStatus(
                    service=name, alive=True, latency_ms=latency,
                    consecutive_failures=0, last_success=time.time(),
                )
                self._heartbeats[name] = status
                return status

            if attempt < len(cfg.RETRY_DELAYS_S) - 1:
                await asyncio.sleep(delay)

        new_fails = prev_fails + 1
        status = HeartbeatStatus(
            service=name,
            alive=False,
            latency_ms=latency,
            consecutive_failures=new_fails,
            last_success=prev.last_success if prev else 0.0,
        )
        self._heartbeats[name] = status

        if new_fails == cfg.MAX_CONSECUTIVE_HEARTBEAT_FAILS:
            primary_rpc = self._heartbeats.get(self._rpc_service_name(cfg.ARB_RPC_URL))
            if name.startswith("rpc:") and name != self._rpc_service_name(cfg.ARB_RPC_URL):
                if primary_rpc and primary_rpc.alive:
                    logger.warning(
                        "Backup RPC degraded: %s failed %d consecutive heartbeats while primary RPC remains healthy",
                        name, new_fails,
                    )
                else:
                    logger.error(
                        "Backup RPC degraded: %s failed %d consecutive heartbeats and primary RPC is not healthy",
                        name, new_fails,
                    )
            elif name == self._rpc_service_name(cfg.ARB_RPC_URL):
                logger.critical(
                    "Primary RPC failed %d consecutive heartbeats: %s",
                    new_fails, name,
                )
            else:
                logger.critical(
                    "Critical service failed %d consecutive heartbeats: %s",
                    new_fails, name,
                )

        return status

    async def run_heartbeat(self) -> dict[str, HeartbeatStatus]:
        """Run one heartbeat cycle across all critical services."""
        tasks = []
        for rpc_url in cfg.RPC_ENDPOINTS:
            name = self._rpc_service_name(rpc_url)
            tasks.append(self._ping_with_retry(name, self._ping_rpc, rpc_url))

        tasks.append(self._ping_with_retry(
            "api:defillama", self._ping_api,
            "https://api.llama.fi/protocols",
        ))

        if cfg.TELEGRAM_TOKEN:
            tasks.append(self._ping_with_retry(
                "api:telegram", self._ping_api,
                f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/getMe",
            ))

        results = await asyncio.gather(*tasks)
        return {r.service: r for r in results}

    # ── Combined Circuit Breaker ──────────────────────────────────────────────

    async def check_circuit_breaker(self) -> dict[str, object]:
        """Wrap existing circuit_breaker.run_circuit_breaker() in async."""
        return await asyncio.to_thread(
            run_circuit_breaker,
            portfolio_usd=self.portfolio_usd,
            verbose=False,
        )

    # ── Full System Health Check ──────────────────────────────────────────────

    async def get_system_health(self) -> SystemHealth:
        """Run all risk checks and return combined health status."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        heartbeats_task = self.run_heartbeat()
        flash_task = self.check_flash_crash()
        cb_task = self.check_circuit_breaker()

        heartbeats, flash, cb = await asyncio.gather(
            heartbeats_task, flash_task, cb_task,
        )

        # Only halt if ALL RPC endpoints are dead, not just backups.
        # Non-RPC services (APIs) still halt individually.
        rpc_heartbeats = {k: v for k, v in heartbeats.items() if k.startswith("rpc:")}
        api_heartbeats = {k: v for k, v in heartbeats.items() if not k.startswith("rpc:")}
        all_rpcs_dead = all(
            hb.consecutive_failures >= cfg.MAX_CONSECUTIVE_HEARTBEAT_FAILS
            for hb in rpc_heartbeats.values()
        ) if rpc_heartbeats else False
        any_api_dead = any(
            hb.consecutive_failures >= cfg.MAX_CONSECUTIVE_HEARTBEAT_FAILS
            for hb in api_heartbeats.values()
        )
        any_heartbeat_dead = all_rpcs_dead or any_api_dead

        cb_all_clear = bool(cb.get("all_clear", False))
        cb_triggered_raw = cb.get("triggered", [])
        cb_triggered = [str(item) for item in cb_triggered_raw] if isinstance(cb_triggered_raw, list) else []

        halted = flash.halted or any_heartbeat_dead or not cb_all_clear

        if halted:
            self.halt_event.set()
        else:
            self.halt_event.clear()

        health = SystemHealth(
            all_healthy=not halted,
            halted=halted,
            heartbeats=heartbeats,
            flash_crash=flash,
            circuit_breaker_clear=cb_all_clear,
            circuit_breaker_triggered=cb_triggered,
            timestamp=ts,
        )

        # Cache for HUD
        cache_data = {
            "all_healthy": health.all_healthy,
            "halted": health.halted,
            "flash_crash": asdict(flash),
            "circuit_breaker_clear": health.circuit_breaker_clear,
            "circuit_breaker_triggered": health.circuit_breaker_triggered,
            "heartbeats": {
                name: {
                    "alive": hb.alive,
                    "latency_ms": hb.latency_ms,
                    "consecutive_failures": hb.consecutive_failures,
                }
                for name, hb in heartbeats.items()
            },
            "timestamp": ts,
        }
        (cfg.CACHE_DIR / "system_health.json").write_text(
            json.dumps(cache_data, indent=2, default=str)
        )

        return health

    def clear_halt(self) -> None:
        """Manually clear the halt state after investigation."""
        self.halt_event.clear()
        logger.info("Halt cleared manually")


# ── Standalone test ───────────────────────────────────────────────────────────

async def _test_risk_manager() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    rm = RiskManager()
    try:
        print(f"\n{'='*60}")
        print("  RISK MANAGER v6.0 — System Health Test")
        print(f"{'='*60}")

        print("\n[1/4] Position sizing (score=70)...")
        pos = await rm.compute_position(signal_score=70)
        print(f"      ${pos.position_usd:,.2f} ({pos.pct_of_portfolio:.1f}%)")
        print(f"      Kelly fraction: {pos.kelly_fraction:.4f}  vol_adj: {pos.vol_adjustment:.4f}")

        print("\n[2/4] Flash-crash check...")
        fc = await rm.check_flash_crash()
        print(f"      price={fc.current_price:.2f} ema={fc.ema_price:.2f} dev={fc.deviation_pct:.2f}%")
        print(f"      halted={fc.halted}")

        print("\n[3/4] Circuit breaker...")
        cb = await rm.check_circuit_breaker()
        print(f"      all_clear={cb['all_clear']} triggered={cb.get('triggered', [])}")

        print("\n[4/4] Heartbeat monitor...")
        hb = await rm.run_heartbeat()
        for name, heartbeat_status in hb.items():
            icon = "OK" if heartbeat_status.alive else "FAIL"
            print(f"      [{icon}] {name}: {heartbeat_status.latency_ms:.0f}ms")

        print(f"\n{'─'*60}")
        health = await rm.get_system_health()
        health_status = "ALL HEALTHY" if health.all_healthy else "HALTED"
        print(f"  System: {health_status}")
        print(f"{'='*60}\n")

    finally:
        await rm.close()


if __name__ == "__main__":
    asyncio.run(_test_risk_manager())

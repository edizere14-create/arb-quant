"""
main_bot.py  (v6.0 — God-Tier Edition)
Asyncio orchestrator: runs telemetry, alpha, and signal loops concurrently.

Usage:
  python main_bot.py              # default (respects PASSIVE_MODE env)
  python main_bot.py --passive    # force shadow-only mode
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import signal
import sys
from dataclasses import asdict
from datetime import datetime, timezone

import httpx

import config as cfg
from alpha_engine import AlphaEngine, GodSignal, AlphaDecayResult
from risk_manager import RiskManager, ActivePosition
from executor import Executor
from shadow_mirror import ShadowMirror
from telegram_menu import TelegramMenuContext, telegram_menu_loop

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main_bot")


# ── Telegram helpers ──────────────────────────────────────────────────────────
_TG_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _tg_escape(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    return _TG_ESCAPE_RE.sub(r"\\\1", str(text))


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value if isinstance(value, (int, float, str)) else default)
    except (TypeError, ValueError):
        return default


async def _send_telegram(client: httpx.AsyncClient, text: str) -> None:
    """Send a MarkdownV2 Telegram message.  Silently no-ops if unconfigured."""
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": cfg.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "MarkdownV2",
    }
    try:
        resp = await client.post(url, json=payload, timeout=10.0)
        if resp.status_code != 200:
            logger.warning("Telegram send failed: %s", resp.text[:200])
    except Exception as exc:
        logger.warning("Telegram send error: %s", exc)


# ── Loop intervals ────────────────────────────────────────────────────────────
TELEMETRY_INTERVAL_S = cfg.HEARTBEAT_INTERVAL_S   # 2.5 s
ALPHA_INTERVAL_S     = cfg.LEAD_LAG_SAMPLE_INTERVAL_S
SIGNAL_INTERVAL_S    = 60.0
TRADE_LOCKOUT_S      = 300
DEFAULT_ASSET        = "ARB"

FNG_API_URL = "https://api.alternative.me/fng/?limit=1"


async def fetch_fear_greed(client: httpx.AsyncClient) -> dict[str, object]:
    """Fetch Crypto Fear & Greed Index. Returns {value, classification, risk_scaler}."""
    try:
        resp = await client.get(FNG_API_URL, timeout=10.0)
        resp.raise_for_status()
        entry = resp.json()["data"][0]
        value = int(entry["value"])
        classification = str(entry["value_classification"])
    except Exception as exc:
        logger.warning("F&G API failed: %s — defaulting to neutral", exc)
        return {"value": 50, "classification": "Unavailable", "risk_scaler": 1.0}

    if value < 25:
        risk_scaler = 0.5
    elif value > 75:
        risk_scaler = 0.75
    else:
        risk_scaler = 1.0

    return {"value": value, "classification": classification, "risk_scaler": risk_scaler}

def _top_lead_lag_rows(raw: dict[str, object], limit: int = 3) -> list[str]:
    lead_lag = raw.get("lead_lag", {}) if isinstance(raw, dict) else {}
    corr_matrix = lead_lag.get("correlation_matrix", {}) if isinstance(lead_lag, dict) else {}
    optimal_lags = lead_lag.get("optimal_lags", {}) if isinstance(lead_lag, dict) else {}
    rows: list[tuple[float, str]] = []

    if not isinstance(corr_matrix, dict):
        return []

    for leader, followers in corr_matrix.items():
        if not isinstance(followers, dict):
            continue
        for follower, corr in followers.items():
            pair_key = f"{leader}->{follower}"
            lag = optimal_lags.get(pair_key, 0) if isinstance(optimal_lags, dict) else 0
            try:
                corr_value = float(corr)
            except (TypeError, ValueError):
                continue
            rows.append((corr_value, f"{pair_key} r={corr_value:.2f} lag={lag}"))

    rows.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in rows[:limit]]


def _format_signal_message(
    raw: dict[str, object],
    pos_pct: float,
    position_usd: float,
    strategy_mode: str,
    risk_scaler: float,
    fng_label: str,
    asset: str,
    side: str,
    passive: bool,
    action_label: str,
) -> str:
    prefix = "\\[SHADOW\\] " if passive else ""
    pair_rows = _top_lead_lag_rows(raw)
    pair_text = "\n".join(
        f"LeadLag: `{_tg_escape(row)}`" for row in pair_rows
    ) if pair_rows else "LeadLag: `warming window`"
    reason = raw.get("reason", "")
    regime = raw.get("regime", "UNKNOWN")
    hurst = raw.get("hurst", 0.0)
    consensus = _as_float(raw.get("consensus_score", 0.0))
    bridge_z = _as_float(raw.get("bridge_z", 0.0))
    sentiment_score = _as_float(raw.get("sentiment_score", 0.0))

    return (
        f"{prefix}🚨 *{action_label}*\n"
        f"Asset: `{_tg_escape(asset)}`\n"
        f"Side: `{_tg_escape(side)}`\n"
        f"Mode: `{_tg_escape(strategy_mode)}`\n"
        f"Size: `${_tg_escape(f'{position_usd:.2f}')}` \\({_tg_escape(f'{pos_pct:.1f}%')}\\)\n"
        f"Risk scaler: `{_tg_escape(f'{risk_scaler:.3f}')}`\n"
        f"F\\&G: `{_tg_escape(fng_label)}`\n"
        f"Consensus: `{_tg_escape(f'{consensus:.4f}')}`\n"
        f"Bridge Z: `{_tg_escape(f'{bridge_z:.4f}')}`\n"
        f"Sentiment: `{_tg_escape(f'{sentiment_score:.4f}')}`\n"
        f"Regime: `{_tg_escape(f'{regime} H={hurst:.4f}')}`\n"
        f"{pair_text}\n"
        f"Reason: `{_tg_escape(str(reason)[:180])}`"
    )


def _format_exit_message(
    asset: str,
    exit_reason: str,
    entry_price: float,
    exit_price: float,
    pnl_usd: float,
    pnl_pct: float,
    trade_id: int,
    passive: bool,
) -> str:
    """Format a Telegram-safe exit notification."""
    prefix = "\\[SHADOW\\] " if passive else ""
    emoji = "🟢" if pnl_usd >= 0 else "🔴"
    return (
        f"{prefix}{emoji} *EXIT — {_tg_escape(exit_reason)}*\n"
        f"Asset: `{_tg_escape(asset)}`\n"
        f"Trade: `#{trade_id}`\n"
        f"Entry: `${_tg_escape(f'{entry_price:.4f}')}`\n"
        f"Exit: `${_tg_escape(f'{exit_price:.4f}')}`\n"
        f"PnL: `${_tg_escape(f'{pnl_usd:+.4f}')}` \\({_tg_escape(f'{pnl_pct:+.2f}%')}\\)"
    )


# ── Telemetry Loop ────────────────────────────────────────────────────────────
async def telemetry_loop(
    risk: RiskManager,
    client: httpx.AsyncClient,
    passive: bool,
) -> None:
    """Dead Man's Switch heartbeats — runs every 2.5s."""
    prefix = "\\[SHADOW\\] " if passive else ""
    logger.info("telemetry_loop started (interval=%.1fs)", TELEMETRY_INTERVAL_S)

    while True:
        try:
            health = await risk.get_system_health()
            if risk.halt_event.is_set():
                msg = (
                    f"{prefix}🚨 *SYSTEM HALT*\n"
                    f"Halted: `True`\n"
                    f"Flash crash: `{_tg_escape(str(health.flash_crash.halted))}`\n"
                    f"CB clear: `{_tg_escape(str(health.circuit_breaker_clear))}`"
                )
                await _send_telegram(client, msg)
                logger.critical("SYSTEM HALTED — waiting for manual intervention")
                # Keep checking in case operator clears the halt
                await asyncio.sleep(30)
                continue
        except Exception as exc:
            logger.error("telemetry error: %s", exc)

        await asyncio.sleep(TELEMETRY_INTERVAL_S)


# ── Alpha Loop ────────────────────────────────────────────────────────────────
async def alpha_loop(
    engine: AlphaEngine,
    risk: RiskManager,
    client: httpx.AsyncClient,
) -> None:
    """Fetch signals every 30s, cache for HUD + signal_loop consumption."""
    logger.info("alpha_loop started (interval=%.0fs)", ALPHA_INTERVAL_S)

    while True:
        if risk.halt_event.is_set():
            await asyncio.sleep(ALPHA_INTERVAL_S)
            continue

        try:
            god_signal = await engine.compute_god_signal()
            logger.info(
                "God-Signal: fires=%s  consensus=%.4f  bridge_z=%.4f  sentiment=%.4f  regime=%s",
                god_signal.fires,
                god_signal.consensus_score,
                god_signal.bridge_z,
                god_signal.sentiment_score,
                god_signal.regime,
            )
        except Exception as exc:
            logger.error("alpha_loop error: %s", exc)

        await asyncio.sleep(ALPHA_INTERVAL_S)


# ── Asset → Pyth pair mapping ─────────────────────────────────────────────────
ASSET_PAIR_MAP: dict[str, str] = {"ARB": "ARB/USD", "ETH": "ETH/USD", "GMX": "GMX/USD"}


# ── Signal Loop ───────────────────────────────────────────────────────────────
async def signal_loop(
    engine: AlphaEngine,
    risk: RiskManager,
    executor: Executor,
    shadow: ShadowMirror,
    client: httpx.AsyncClient,
    passive: bool,
) -> None:
    """
    Every 60s: check latest God-Signal.
    If fires → risk check → execute (TWAP) or shadow-record.
    """
    prefix = "\\[SHADOW\\] " if passive else ""
    logger.info("signal_loop started (interval=%.0fs, passive=%s)", SIGNAL_INTERVAL_S, passive)
    asset = DEFAULT_ASSET
    side = "BUY"

    # Warm the rolling lead-lag window before consuming cached alpha.
    await asyncio.sleep(max(ALPHA_INTERVAL_S + 5, engine.required_window_seconds))

    while True:
        if risk.halt_event.is_set():
            await asyncio.sleep(SIGNAL_INTERVAL_S)
            continue

        try:
            # ── EXIT CHECK — evaluate active positions every tick ──────────
            pos_obj = risk.get_position(asset)
            if pos_obj is not None:
                pair = ASSET_PAIR_MAP.get(asset, f"{asset}/USD")
                current_price = await engine.get_latest_asset_price(pair)

                if current_price is not None:
                    pos_obj.update_trailing(current_price)

                    # Exit condition 1: hard stop / trailing stop hit
                    should_exit, exit_reason = pos_obj.check_exit(current_price)

                    # Exit condition 2: alpha decay overrides take-profit
                    if not should_exit:
                        cached_gs = cfg.CACHE_DIR / "god_signal.json"
                        bridge_z_now = 0.0
                        if cached_gs.exists():
                            gs_data = json.loads(cached_gs.read_text())
                            bridge_z_now = _as_float(gs_data.get("bridge_z", 0.0))

                        decay = engine.check_alpha_decay(asset, bridge_z=bridge_z_now)
                        if decay.emergency_exit:
                            should_exit = True
                            exit_reason = f"ALPHA_DECAY ({decay.reason})"

                    if should_exit:
                        logger.warning(
                            "EXIT triggered for %s trade#%d: %s | price=$%.4f stop=$%.4f tp=$%.4f",
                            asset, pos_obj.trade_id, exit_reason,
                            current_price, pos_obj.trailing_stop_price, pos_obj.take_profit_price,
                        )

                        # Close the trade in DB
                        close_result = await shadow.close_trade(pos_obj.trade_id, current_price)
                        pnl_usd = float(close_result.get("pnl_usd", 0.0))
                        pnl_pct = float(close_result.get("pnl_pct", 0.0))

                        # Clear position state
                        risk.close_position(asset)
                        risk.register_trade(asset=asset, side="SELL", cooldown_seconds=TRADE_LOCKOUT_S)
                        risk.sync_asset_state(asset, await shadow.get_position_state(asset))

                        logger.info(
                            "Position CLOSED: %s trade#%d PnL=$%.4f (%.2f%%) reason=%s",
                            asset, pos_obj.trade_id, pnl_usd, pnl_pct, exit_reason,
                        )

                        # Telegram exit alert
                        exit_msg = _format_exit_message(
                            asset=asset,
                            exit_reason=exit_reason,
                            entry_price=pos_obj.entry_price,
                            exit_price=current_price,
                            pnl_usd=pnl_usd,
                            pnl_pct=pnl_pct,
                            trade_id=pos_obj.trade_id,
                            passive=passive,
                        )
                        await _send_telegram(client, exit_msg)

                        # Refresh portfolio snapshot
                        if passive:
                            await shadow.get_portfolio_snapshot()

                        await asyncio.sleep(SIGNAL_INTERVAL_S)
                        continue

                    # No exit — log trailing status
                    logger.info(
                        "Position %s trade#%d: price=$%.4f high=$%.4f stop=$%.4f tp=$%.4f",
                        asset, pos_obj.trade_id, current_price,
                        pos_obj.highest_price_reached, pos_obj.trailing_stop_price,
                        pos_obj.take_profit_price,
                    )

                else:
                    logger.warning("Failed to fetch price for %s — skipping exit check", pair)

            # ── ENTRY CHECK — look for new BUY signals ────────────────────
            # Read latest cached god signal
            cached = cfg.CACHE_DIR / "god_signal.json"
            if not cached.exists():
                await asyncio.sleep(SIGNAL_INTERVAL_S)
                continue

            raw = json.loads(cached.read_text())
            fires = raw.get("fires", False)

            if not fires:
                logger.info("Signal check: NO FIRE — %s", raw.get("reason", "")[:80])
                await asyncio.sleep(SIGNAL_INTERVAL_S)
                continue

            persisted_state = await shadow.get_position_state(asset)
            risk.sync_asset_state(asset, persisted_state)
            locked, lock_reason = risk.is_trade_locked(asset=asset, side=side)
            if locked:
                logger.info("Signal check: SKIP %s %s — %s", side, asset, lock_reason)
                await asyncio.sleep(SIGNAL_INTERVAL_S)
                continue

            # ── God-Signal fired — risk gate ──────────────────────────────────
            logger.info("GOD-SIGNAL FIRES — running risk checks")

            consensus = raw.get("consensus_score", 0.0)
            bridge_z = raw.get("bridge_z", 0.0)
            sentiment_score = raw.get("sentiment_score", 0.0)

            # Map consensus to 0-100 signal score for position sizer
            signal_score = int(min(100, max(0, consensus * 100)))

            current_portfolio = cfg.PORTFOLIO_USD
            active_positions = sum(1 for is_active in risk.active_positions.values() if is_active)
            if passive:
                snapshot_before = await shadow.get_portfolio_snapshot()
                current_portfolio = float(snapshot_before.get("total_value", cfg.PORTFOLIO_USD))
                risk.update_portfolio(current_portfolio)
                active_positions = sum(1 for is_active in risk.active_positions.values() if is_active)

            pos = await risk.compute_position(
                signal_score=signal_score,
                active_positions=active_positions,
                portfolio_usd=current_portfolio,
            )
            if not pos.tradeable:
                logger.info("Position sizer says NOT TRADEABLE (score=%d)", signal_score)
                await asyncio.sleep(SIGNAL_INTERVAL_S)
                continue

            size_usd = pos.position_usd

            # ── Fear & Greed sentiment scaler ─────────────────────────────
            fng = await fetch_fear_greed(client)
            fng_value = int(fng["value"])
            fng_cls = str(fng["classification"])
            fng_scaler = float(fng["risk_scaler"])

            size_usd *= fng_scaler
            fng_label = f"{fng_cls} - {fng_value} (x{fng_scaler})"
            logger.info(
                "F&G: %d (%s) → risk_scaler=%.2f → adjusted size=$%.2f",
                fng_value, fng_cls, fng_scaler, size_usd,
            )

            # Fetch entry price from Pyth
            pair = ASSET_PAIR_MAP.get(asset, f"{asset}/USD")
            entry_price = await engine.get_latest_asset_price(pair) or 1.0

            # ── Execute or Shadow ─────────────────────────────────────────────
            if passive:
                trade_id = await shadow.record_trade(
                    signal_type="GOD_SIGNAL",
                    asset=asset,
                    side=side,
                    price=entry_price,
                    quantity=size_usd,
                    god_signal_score=consensus,
                    bridge_z=bridge_z,
                    sentiment=sentiment_score,
                    notes=f"consensus={consensus:.4f}",
                )
                risk.register_trade(asset=asset, side=side, cooldown_seconds=TRADE_LOCKOUT_S)
                risk.sync_asset_state(asset, await shadow.get_position_state(asset))

                # Create ActivePosition for exit tracking
                risk.open_position(asset=asset, trade_id=trade_id, entry_price=entry_price, position_size=size_usd)

                logger.info("[SHADOW] Paper trade #%d: %s %s $%.2f @ $%.4f", trade_id, side, asset, size_usd, entry_price)

                # Snapshot portfolio
                await shadow.get_portfolio_snapshot()

                msg = _format_signal_message(
                    raw=raw,
                    pos_pct=pos.pct_of_portfolio,
                    position_usd=size_usd,
                    strategy_mode=pos.strategy_mode,
                    risk_scaler=pos.risk_scaler,
                    fng_label=fng_label,
                    asset=asset,
                    side=side,
                    passive=True,
                    action_label="Paper Trade",
                )
                await _send_telegram(client, msg)

            else:
                # Active mode — TWAP via Flashbots
                fb_ok = await executor.check_flashbots()
                if not fb_ok:
                    logger.critical("Flashbots unreachable — HARD BLOCK")
                    await _send_telegram(client, "🚨 *Flashbots unreachable* — execution blocked")
                    await asyncio.sleep(SIGNAL_INTERVAL_S)
                    continue

                result = await executor.execute_twap(
                    asset=asset,
                    side=side,
                    total_usd=size_usd,
                )
                slices_executed = sum(1 for slice_result in result.slices if slice_result.status == "EXECUTED")
                slices_total = len(result.slices)
                rpc_used = result.rpc_node.url
                logger.info(
                    "TWAP execution: %s %s $%.2f — %d/%d slices OK",
                    side, asset, size_usd,
                    slices_executed, slices_total,
                )
                if slices_executed > 0:
                    await shadow.mark_trade_state(asset=asset, side=side, cooldown_seconds=TRADE_LOCKOUT_S)
                    risk.register_trade(asset=asset, side=side, cooldown_seconds=TRADE_LOCKOUT_S)
                    risk.sync_asset_state(asset, await shadow.get_position_state(asset))

                    # Create ActivePosition for exit tracking
                    risk.open_position(asset=asset, trade_id=0, entry_price=entry_price, position_size=size_usd)

                msg = _format_signal_message(
                    raw=raw,
                    pos_pct=pos.pct_of_portfolio,
                    position_usd=size_usd,
                    strategy_mode=pos.strategy_mode,
                    risk_scaler=pos.risk_scaler,
                    fng_label=fng_label,
                    asset=asset,
                    side=side,
                    passive=False,
                    action_label="TWAP Executed",
                ) + (
                    f"\nSlices: `{slices_executed}/{slices_total}`"
                    f"\nRPC: `{_tg_escape(rpc_used[:30])}`"
                )
                await _send_telegram(client, msg)

        except Exception as exc:
            logger.error("signal_loop error: %s", exc)

        await asyncio.sleep(SIGNAL_INTERVAL_S)


# ── Main ──────────────────────────────────────────────────────────────────────
async def run(passive: bool) -> None:
    mode = "PASSIVE (shadow)" if passive else "ACTIVE (live)"
    logger.info("=" * 60)
    logger.info("  Arbitrum Alpha Operator v6.0 — %s", mode)
    logger.info("  Portfolio: $%s", f"{cfg.PORTFOLIO_USD:,.0f}")
    logger.info("  RPCs: %s", ", ".join(cfg.RPC_ENDPOINTS))
    logger.info("=" * 60)

    # Validate config
    issues = cfg.validate_config(require_private_key=not passive)
    if issues:
        for issue in issues:
            logger.error("CONFIG: %s", issue)
        sys.exit(1)

    # Mutable passive flag — telegram /mode command can toggle this
    _passive_state = {"value": passive}

    def _get_passive() -> bool:
        return _passive_state["value"]

    def _set_passive(val: bool) -> None:
        _passive_state["value"] = val
        logger.info("Passive mode set to %s", val)

    # Initialize components
    halt_event = asyncio.Event()
    engine  = AlphaEngine()
    risk    = RiskManager(portfolio_usd=cfg.PORTFOLIO_USD, halt_event=halt_event)
    executor_inst = Executor(passive=passive)
    shadow  = ShadowMirror()
    client  = httpx.AsyncClient(headers={"User-Agent": "arb-quant/6.0"}, follow_redirects=True)

    if passive:
        await shadow.get_portfolio_snapshot()

    # Pre-flight RPC ping
    nodes = await executor_inst.ping_all_rpcs()
    alive = [n for n in nodes if n.alive]
    logger.info("RPC status: %d/%d alive", len(alive), len(nodes))

    if not alive:
        logger.critical("No RPC endpoints reachable — aborting")
        sys.exit(1)

    # Startup Telegram
    startup_msg = (
        f"{'\\[SHADOW\\] ' if passive else ''}🟢 *Bot Started*\n"
        f"Mode: `{_tg_escape(mode)}`\n"
        f"Portfolio: `${_tg_escape(f'{cfg.PORTFOLIO_USD:,.0f}')}`\n"
        f"RPCs alive: `{len(alive)}/{len(nodes)}`"
    )
    await _send_telegram(client, startup_msg)

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # Telegram command menu context
    tg_ctx = TelegramMenuContext(
        risk=risk,
        shadow=shadow,
        engine=engine,
        halt_event=halt_event,
        get_passive=_get_passive,
        set_passive=_set_passive,
    )

    # Launch concurrent loops
    tasks = [
        asyncio.create_task(
            telemetry_loop(risk, client, passive),
            name="telemetry",
        ),
        asyncio.create_task(
            alpha_loop(engine, risk, client),
            name="alpha",
        ),
        asyncio.create_task(
            signal_loop(engine, risk, executor_inst, shadow, client, passive),
            name="signal",
        ),
        asyncio.create_task(
            telegram_menu_loop(tg_ctx),
            name="telegram_menu",
        ),
    ]

    try:
        # Wait for shutdown signal or first task failure
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task.exception():
                logger.error("Task %s failed: %s", task.get_name(), task.exception())
    except asyncio.CancelledError:
        pass
    finally:
        # Teardown
        logger.info("Shutting down...")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await engine.close()
        await risk.close()
        await executor_inst.close()
        await shadow.close()
        await client.aclose()

        shutdown_msg = f"{'\\[SHADOW\\] ' if passive else ''}🔴 *Bot Stopped*"
        # Use a fresh client for final message
        async with httpx.AsyncClient() as c:
            await _send_telegram(c, shutdown_msg)

        logger.info("Shutdown complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="Arbitrum Alpha Operator v6.0")
    parser.add_argument(
        "--passive",
        action="store_true",
        help="Run in shadow/paper-trading mode (no real transactions)",
    )
    args = parser.parse_args()

    passive = args.passive or cfg.is_passive_mode()

    try:
        asyncio.run(run(passive))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

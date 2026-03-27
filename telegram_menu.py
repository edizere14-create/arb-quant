"""
telegram_menu.py — Persistent Telegram Command Menu
Polls for updates via getUpdates and responds to button presses + /mode command.

Buttons (ReplyKeyboardMarkup — always visible):
  📊 Status    — system health, RPC latency, last God-Signal conviction
  💰 Positions — active strategy monitor table
  📈 Report    — 24h ROI and portfolio balance progress
  🛑 Emergency Stop — triggers inline confirm/cancel

Inline callbacks:
  confirm_halt — sets halt, closes all positions, sends SYSTEM DARK
  cancel_halt  — cancels the emergency stop

/mode — toggles PASSIVE ↔ ACTIVE on the fly
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

import config as cfg

logger = logging.getLogger("telegram_menu")

_TG_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _esc(text: str) -> str:
    return _TG_ESCAPE_RE.sub(r"\\\1", str(text))


# ── Telegram Bot API helpers ──────────────────────────────────────────────────

_BASE = "https://api.telegram.org/bot{token}"


async def _api(
    client: httpx.AsyncClient,
    method: str,
    **kwargs: Any,
) -> dict[str, Any]:
    url = f"{_BASE.format(token=cfg.TELEGRAM_TOKEN)}/{method}"
    try:
        resp = await client.post(url, json=kwargs, timeout=30.0)
        data: dict[str, Any] = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram API %s failed: %s", method, data.get("description", ""))
        return data
    except Exception as exc:
        logger.warning("Telegram API %s error: %s", method, exc)
        return {"ok": False}


async def _send(
    client: httpx.AsyncClient,
    chat_id: str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
    }
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return await _api(client, "sendMessage", **kwargs)


async def _answer_callback(
    client: httpx.AsyncClient,
    callback_query_id: str,
    text: str = "",
) -> None:
    await _api(client, "answerCallbackQuery", callback_query_id=callback_query_id, text=text)


async def _edit_message(
    client: httpx.AsyncClient,
    chat_id: str,
    message_id: int,
    text: str,
) -> None:
    await _api(
        client, "editMessageText",
        chat_id=chat_id, message_id=message_id,
        text=text, parse_mode="MarkdownV2",
    )


# ── Persistent ReplyKeyboard ─────────────────────────────────────────────────

MENU_KEYBOARD: dict[str, Any] = {
    "keyboard": [
        [{"text": "📊 Status"}, {"text": "💰 Positions"}],
        [{"text": "📈 Report"}, {"text": "🛑 Emergency Stop"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


# ── Handler context ───────────────────────────────────────────────────────────

class TelegramMenuContext:
    """Holds references to bot components so handlers can query state."""

    def __init__(
        self,
        risk: Any,          # RiskManager
        shadow: Any,        # ShadowMirror
        engine: Any,        # AlphaEngine
        halt_event: asyncio.Event,
        get_passive: Any,   # callable returning bool
        set_passive: Any,   # callable accepting bool
    ):
        self.risk = risk
        self.shadow = shadow
        self.engine = engine
        self.halt_event = halt_event
        self.get_passive = get_passive
        self.set_passive = set_passive


# ── Handlers ──────────────────────────────────────────────────────────────────

async def _handle_status(client: httpx.AsyncClient, chat_id: str, ctx: TelegramMenuContext) -> None:
    """📊 Status — system health, RPC latency, last God-Signal conviction."""
    try:
        health = await ctx.risk.get_system_health()
        halted = health.halted
        status_icon = "🔴" if halted else "🟢"

        # RPC latencies
        rpc_lines: list[str] = []
        for name, hb in health.heartbeats.items():
            icon = "🟢" if hb.alive else "🔴"
            rpc_lines.append(f"  {icon} `{_esc(name[:25])}` {_esc(f'{hb.latency_ms:.0f}')}ms")
        rpc_text = "\n".join(rpc_lines) if rpc_lines else "  No heartbeats"

        # Last God-Signal
        gs_path = cfg.CACHE_DIR / "god_signal.json"
        consensus = 0.0
        bridge_z = 0.0
        fires = False
        gs_ts = "N/A"
        if gs_path.exists():
            gs = json.loads(gs_path.read_text())
            consensus = float(gs.get("consensus_score", 0.0))
            bridge_z = float(gs.get("bridge_z", 0.0))
            fires = bool(gs.get("fires", False))
            gs_ts = str(gs.get("timestamp", "N/A"))

        signal_icon = "🟢" if fires else "🔴"
        mode = "PASSIVE" if ctx.get_passive() else "ACTIVE"

        text = (
            f"{status_icon} *System Status*\n\n"
            f"Halted: `{_esc(str(halted))}`\n"
            f"Mode: `{_esc(mode)}`\n"
            f"Flash Crash: `{_esc(str(health.flash_crash.halted))}`\n"
            f"Circuit Breaker: `{_esc(str(health.circuit_breaker_clear))}`\n\n"
            f"*RPC Latency:*\n{rpc_text}\n\n"
            f"*Last God\\-Signal:*\n"
            f"  {signal_icon} Fires: `{_esc(str(fires))}`\n"
            f"  Consensus: `{_esc(f'{consensus:.4f}')}`\n"
            f"  Bridge Z: `{_esc(f'{bridge_z:.4f}')}`\n"
            f"  Time: `{_esc(gs_ts)}`"
        )
    except Exception as exc:
        text = f"🔴 *Status Error*\n`{_esc(str(exc)[:200])}`"

    await _send(client, chat_id, text, reply_markup=MENU_KEYBOARD)


async def _handle_positions(client: httpx.AsyncClient, chat_id: str, ctx: TelegramMenuContext) -> None:
    """💰 Positions — Active Strategy Monitor table."""
    try:
        open_trades = await ctx.shadow.get_open_trades()

        if not open_trades:
            text = "💰 *Active Positions*\n\n_No open positions_"
            await _send(client, chat_id, text, reply_markup=MENU_KEYBOARD)
            return

        lines: list[str] = ["💰 *Active Strategy Monitor*\n"]
        lines.append("`Asset  | Entry    | PnL%     | Stop`")
        lines.append("`" + _esc("-" * 38) + "`")

        for trade in open_trades[:15]:  # cap display
            asset = str(trade.get("asset", "?"))
            entry_price = float(trade.get("price", 0.0))
            trade_id = int(trade.get("id", 0))

            # Get live position data if tracked
            pos_obj = ctx.risk.get_position(asset)
            if pos_obj and pos_obj.trade_id == trade_id:
                stop = pos_obj.trailing_stop_price
                stop_icon = "🛡️" if pos_obj.trailing_stop_price > pos_obj.hard_stop_price else ""

                # Try to get current price for PnL
                pair = {"ARB": "ARB/USD", "ETH": "ETH/USD", "GMX": "GMX/USD"}.get(asset, f"{asset}/USD")
                current_price = await ctx.engine.get_latest_asset_price(pair)
                if current_price and entry_price > 0:
                    pnl_pct = (current_price - entry_price) / entry_price * 100
                else:
                    pnl_pct = 0.0

                pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"
                lines.append(
                    f"{pnl_icon} `{_esc(asset[:6]):<6} | "
                    f"${_esc(f'{entry_price:.4f}'):>8} | "
                    f"{_esc(f'{pnl_pct:+.2f}%'):>8} | "
                    f"${_esc(f'{stop:.4f}')}` {stop_icon}"
                )
            else:
                lines.append(
                    f"  `{_esc(asset[:6]):<6} | "
                    f"${_esc(f'{entry_price:.4f}'):>8} | "
                    f"{'N/A':>8} | "
                    f"{'N/A':>8}`"
                )

        text = "\n".join(lines)
    except Exception as exc:
        text = f"🔴 *Positions Error*\n`{_esc(str(exc)[:200])}`"

    await _send(client, chat_id, text, reply_markup=MENU_KEYBOARD)


async def _handle_report(client: httpx.AsyncClient, chat_id: str, ctx: TelegramMenuContext) -> None:
    """📈 Report — 24h ROI and portfolio balance progress."""
    try:
        snapshot = await ctx.shadow.get_portfolio_snapshot()
        total_value = float(snapshot.get("total_value", cfg.PORTFOLIO_USD))
        cash = float(snapshot.get("cash", 0.0))
        positions_value = float(snapshot.get("positions_value", 0.0))
        drawdown_pct = float(snapshot.get("drawdown_pct", 0.0))
        sharpe = snapshot.get("sharpe_rolling")

        # Progress bar $1,000 → $5,000
        base = cfg.PORTFOLIO_USD
        target = cfg.MAX_PORTFOLIO_USD
        progress = max(0.0, min(1.0, (total_value - base) / (target - base))) if target > base else 1.0
        bar_len = 20
        filled = int(progress * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        # 24h ROI from closed trades
        history = await ctx.shadow.get_trade_history(limit=200)
        now = datetime.now(timezone.utc)
        pnl_24h = 0.0
        trades_24h = 0
        for t in history:
            if t.get("status") != "CLOSED" or not t.get("exit_timestamp"):
                continue
            try:
                exit_dt = datetime.fromisoformat(str(t["exit_timestamp"]).replace("Z", "+00:00"))
                if (now - exit_dt).total_seconds() <= 86400:
                    pnl_24h += float(t.get("pnl_usd", 0.0))
                    trades_24h += 1
            except (ValueError, TypeError):
                continue

        roi_24h = (pnl_24h / base * 100) if base > 0 else 0.0
        roi_icon = "🟢" if roi_24h >= 0 else "🔴"
        sharpe_text = f"{sharpe:.4f}" if sharpe is not None else "N/A"

        text = (
            f"📈 *Portfolio Report*\n\n"
            f"*Balance:* `${_esc(f'{total_value:,.2f}')}`\n"
            f"  Cash: `${_esc(f'{cash:,.2f}')}`\n"
            f"  Positions: `${_esc(f'{positions_value:,.2f}')}`\n\n"
            f"*Progress \\($1,000 → $5,000\\):*\n"
            f"`{_esc(bar)}` {_esc(f'{progress*100:.1f}%')}\n\n"
            f"*24h Performance:*\n"
            f"  {roi_icon} ROI: `{_esc(f'{roi_24h:+.2f}%')}`\n"
            f"  PnL: `${_esc(f'{pnl_24h:+.2f}')}`\n"
            f"  Trades: `{trades_24h}`\n\n"
            f"Drawdown: `{_esc(f'{drawdown_pct:.2f}%')}`\n"
            f"Rolling Sharpe: `{_esc(sharpe_text)}`"
        )
    except Exception as exc:
        text = f"🔴 *Report Error*\n`{_esc(str(exc)[:200])}`"

    await _send(client, chat_id, text, reply_markup=MENU_KEYBOARD)


async def _handle_emergency_stop(client: httpx.AsyncClient, chat_id: str) -> None:
    """🛑 Emergency Stop — send confirmation inline buttons."""
    text = "🛑 *EMERGENCY STOP*\n\nAre you sure\\? This will halt all trading and close every open position\\."
    inline_kb: dict[str, Any] = {
        "inline_keyboard": [
            [
                {"text": "‼️ CONFIRM HALT ‼️", "callback_data": "confirm_halt"},
                {"text": "CANCEL", "callback_data": "cancel_halt"},
            ]
        ]
    }
    await _send(client, chat_id, text, reply_markup=inline_kb)


async def _handle_confirm_halt(
    client: httpx.AsyncClient,
    chat_id: str,
    message_id: int,
    callback_query_id: str,
    ctx: TelegramMenuContext,
) -> None:
    """Execute emergency halt: close all positions, set halt flag."""
    await _answer_callback(client, callback_query_id, text="⚠️ HALTING...")

    # Set halt
    ctx.halt_event.set()
    logger.critical("EMERGENCY HALT triggered via Telegram")

    # Close all open positions at market price
    closed_count = 0
    closed_pnl = 0.0
    try:
        open_trades = await ctx.shadow.get_open_trades()
        for trade in open_trades:
            trade_id = int(trade.get("id", 0))
            asset = str(trade.get("asset", ""))
            pair = {"ARB": "ARB/USD", "ETH": "ETH/USD", "GMX": "GMX/USD"}.get(asset, f"{asset}/USD")
            exit_price = await ctx.engine.get_latest_asset_price(pair)
            if exit_price is None:
                exit_price = float(trade.get("price", 0.0))  # fallback to entry
            result = await ctx.shadow.close_trade(trade_id, exit_price)
            if "error" not in result:
                closed_count += 1
                closed_pnl += float(result.get("pnl_usd", 0.0))
                ctx.risk.close_position(asset)
                ctx.risk.register_trade(asset=asset, side="SELL", cooldown_seconds=3600)
    except Exception as exc:
        logger.error("Emergency close error: %s", exc)

    # Edit the original message
    await _edit_message(
        client, chat_id, message_id,
        f"🔴 *SYSTEM DARK*\n\n"
        f"All trading halted\\.\n"
        f"Positions closed: `{closed_count}`\n"
        f"Realized PnL: `${_esc(f'{closed_pnl:+.2f}')}`\n\n"
        f"_Use /mode to resume after investigation\\._",
    )

    # Broadcast SYSTEM DARK
    await _send(
        client, chat_id,
        "🔴🔴🔴 *SYSTEM DARK* 🔴🔴🔴\n\n"
        f"Emergency halt executed\\.\n"
        f"Closed `{closed_count}` positions\\.\n"
        f"Total PnL: `${_esc(f'{closed_pnl:+.2f}')}`\n\n"
        f"_Bot is now halted\\. Manual intervention required\\._",
        reply_markup=MENU_KEYBOARD,
    )


async def _handle_cancel_halt(
    client: httpx.AsyncClient,
    chat_id: str,
    message_id: int,
    callback_query_id: str,
) -> None:
    await _answer_callback(client, callback_query_id, text="Cancelled")
    await _edit_message(
        client, chat_id, message_id,
        "🟢 *Emergency Stop Cancelled*\n\n_Operations continue normally\\._",
    )


async def _handle_mode(
    client: httpx.AsyncClient,
    chat_id: str,
    ctx: TelegramMenuContext,
    args: str,
) -> None:
    """/mode [passive|active] — toggle or set mode."""
    args = args.strip().lower()
    current = ctx.get_passive()

    if args in ("passive", "shadow"):
        ctx.set_passive(True)
    elif args in ("active", "live"):
        ctx.set_passive(False)
    elif args == "":
        # Toggle
        ctx.set_passive(not current)
    else:
        await _send(
            client, chat_id,
            f"⚠️ Unknown mode `{_esc(args)}`\\. Use `/mode passive` or `/mode active`\\.",
            reply_markup=MENU_KEYBOARD,
        )
        return

    new_mode = "PASSIVE" if ctx.get_passive() else "ACTIVE"
    icon = "🟢" if new_mode == "PASSIVE" else "🔴"

    # Clear halt when switching to active from halted state
    if not ctx.get_passive() and ctx.halt_event.is_set():
        ctx.halt_event.clear()
        logger.info("Halt cleared via /mode active")

    await _send(
        client, chat_id,
        f"{icon} *Mode Changed*\n\nNow running in `{_esc(new_mode)}` mode\\.",
        reply_markup=MENU_KEYBOARD,
    )
    logger.info("Mode changed to %s via Telegram", new_mode)


# ── Main polling loop ─────────────────────────────────────────────────────────

async def telegram_menu_loop(ctx: TelegramMenuContext) -> None:
    """Long-poll Telegram getUpdates and dispatch to handlers."""
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        logger.info("Telegram menu disabled — no token/chat_id configured")
        return

    allowed_chat = cfg.TELEGRAM_CHAT_ID
    offset = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "arb-quant/6.0"},
        follow_redirects=True,
    ) as client:
        # Set persistent menu on startup
        await _send(
            client, allowed_chat,
            "🟢 *Menu Active*\n_Use the buttons below to control the bot\\._",
            reply_markup=MENU_KEYBOARD,
        )

        logger.info("telegram_menu_loop started — polling for updates")

        while True:
            try:
                data = await _api(
                    client, "getUpdates",
                    offset=offset, timeout=25, allowed_updates=["message", "callback_query"],
                )
                results = data.get("result", [])
                if not isinstance(results, list):
                    await asyncio.sleep(2)
                    continue

                for update in results:
                    update_id = int(update.get("update_id", 0))
                    offset = max(offset, update_id + 1)

                    # ── Callback queries (inline buttons) ─────────────────
                    cb = update.get("callback_query")
                    if cb:
                        cb_chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                        if cb_chat_id != allowed_chat:
                            continue
                        cb_data = cb.get("data", "")
                        cb_id = cb.get("id", "")
                        msg_id = int(cb.get("message", {}).get("message_id", 0))

                        if cb_data == "confirm_halt":
                            await _handle_confirm_halt(client, cb_chat_id, msg_id, cb_id, ctx)
                        elif cb_data == "cancel_halt":
                            await _handle_cancel_halt(client, cb_chat_id, msg_id, cb_id)
                        continue

                    # ── Text messages / commands ──────────────────────────
                    msg = update.get("message")
                    if not msg:
                        continue
                    msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                    if msg_chat_id != allowed_chat:
                        continue
                    text = str(msg.get("text", "")).strip()

                    if text == "📊 Status":
                        await _handle_status(client, msg_chat_id, ctx)
                    elif text == "💰 Positions":
                        await _handle_positions(client, msg_chat_id, ctx)
                    elif text == "📈 Report":
                        await _handle_report(client, msg_chat_id, ctx)
                    elif text == "🛑 Emergency Stop":
                        await _handle_emergency_stop(client, msg_chat_id)
                    elif text.startswith("/mode"):
                        args_text = text[len("/mode"):].strip()
                        await _handle_mode(client, msg_chat_id, ctx, args_text)
                    elif text == "/start":
                        await _send(
                            client, msg_chat_id,
                            "🟢 *Arbitrum Alpha Operator v6\\.0*\n\n"
                            "Use the menu below to control the bot\\.",
                            reply_markup=MENU_KEYBOARD,
                        )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("telegram_menu_loop error: %s", exc)
                await asyncio.sleep(5)

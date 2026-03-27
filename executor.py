"""
executor.py  (v6.0 — God-Tier Edition)
Async execution engine with RPC rotation and TWAP order splitting.

Features:
  A. RPC Rotation   — pre-flight ping, select lowest latency (<150ms)
  B. TWAP Execution — split orders into 3 parts for slippage/MEV defense
  C. Flashbots-only — no public mempool submission (hard block)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

import config as cfg

logger = logging.getLogger("executor")


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RPCNode:
    url: str
    latency_ms: float
    block_number: int
    alive: bool


@dataclass(frozen=True)
class TWAPSlice:
    index: int
    total_slices: int
    amount_usd: float
    rpc_used: str
    timestamp: str
    tx_hash: str | None   # None in passive mode
    status: str           # "EXECUTED" | "SHADOW" | "FAILED"


@dataclass(frozen=True)
class ExecutionResult:
    asset: str
    side: str
    total_usd: float
    slices: list[TWAPSlice]
    rpc_node: RPCNode
    is_passive: bool
    timestamp: str


# ── Executor ──────────────────────────────────────────────────────────────────

class Executor:
    """
    Async execution engine.
    - Rotates across 3 RPC endpoints, selects lowest latency
    - Splits orders into TWAP_SPLITS (3) parts
    - In passive mode: logs intended trades without submitting
    - In active mode: signs via Flashbots Protect (no public mempool)
    """

    def __init__(self, passive: bool = True):
        self.passive = passive
        self._nodes: list[RPCNode] = []
        self._best_rpc: RPCNode | None = None
        self._last_ping: float = 0.0
        self._http_client: httpx.AsyncClient | None = None
        self._nonce_lock = asyncio.Lock()

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

    # ── RPC Rotation ──────────────────────────────────────────────────────────

    async def _ping_rpc(self, url: str) -> RPCNode:
        client = await self._get_client()
        start = time.monotonic()
        try:
            resp = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                timeout=5.0,
            )
            elapsed_ms = (time.monotonic() - start) * 1000
            data = resp.json()
            block = int(data.get("result", "0x0"), 16)
            return RPCNode(url=url, latency_ms=round(elapsed_ms, 1), block_number=block, alive=True)
        except Exception:
            elapsed_ms = (time.monotonic() - start) * 1000
            return RPCNode(url=url, latency_ms=round(elapsed_ms, 1), block_number=0, alive=False)

    async def ping_all_rpcs(self) -> list[RPCNode]:
        """Ping all configured RPC endpoints and rank by latency."""
        tasks = [self._ping_rpc(url) for url in cfg.RPC_ENDPOINTS]
        nodes = await asyncio.gather(*tasks)
        self._nodes = sorted(nodes, key=lambda n: (not n.alive, n.latency_ms))
        self._last_ping = time.time()

        alive_nodes = [n for n in self._nodes if n.alive]
        if alive_nodes:
            self._best_rpc = alive_nodes[0]
            if self._best_rpc.latency_ms > cfg.MAX_RPC_LATENCY_MS:
                logger.warning(
                    "Best RPC latency %.0fms exceeds %dms threshold (degraded mode)",
                    self._best_rpc.latency_ms, cfg.MAX_RPC_LATENCY_MS,
                )
        else:
            self._best_rpc = None
            logger.critical("All RPC endpoints unreachable")

        return list(self._nodes)

    async def get_best_rpc(self) -> RPCNode | None:
        """Get the current best RPC node. Re-pings if stale."""
        if time.time() - self._last_ping > cfg.RPC_PING_INTERVAL_S:
            await self.ping_all_rpcs()
        return self._best_rpc

    # ── Flashbots Gate ────────────────────────────────────────────────────────

    async def check_flashbots(self) -> bool:
        """HARD BLOCK — Flashbots must be reachable for active mode."""
        if self.passive:
            return True  # not needed in shadow mode
        node = await self._ping_rpc(cfg.FLASHBOTS_RPC)
        if not node.alive:
            logger.critical("Flashbots RPC unreachable — CANNOT execute (hard block)")
        return node.alive

    # ── TWAP Execution ────────────────────────────────────────────────────────

    async def execute_twap(
        self,
        asset: str,
        side: str,
        total_usd: float,
        shadow_callback=None,
    ) -> ExecutionResult:
        """
        Split order into TWAP_SPLITS parts and execute sequentially.
        In passive mode, calls shadow_callback instead of submitting on-chain.

        Args:
            asset: Token symbol (e.g. "ARB")
            side: "BUY" or "SELL"
            total_usd: Total order size in USD
            shadow_callback: async fn(asset, side, slice_usd, price) for shadow mode
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        best = await self.get_best_rpc()

        if best is None:
            logger.error("No RPC available — aborting execution")
            return ExecutionResult(
                asset=asset, side=side, total_usd=total_usd,
                slices=[], rpc_node=RPCNode("none", 0, 0, False),
                is_passive=self.passive, timestamp=ts,
            )

        # Active mode requires Flashbots
        if not self.passive:
            fb_ok = await self.check_flashbots()
            if not fb_ok:
                logger.error("Flashbots gate FAILED — aborting (no public mempool fallback)")
                return ExecutionResult(
                    asset=asset, side=side, total_usd=total_usd,
                    slices=[], rpc_node=best,
                    is_passive=self.passive, timestamp=ts,
                )

        slice_usd = total_usd / cfg.TWAP_SPLITS
        slices: list[TWAPSlice] = []
        delay_s = cfg.TWAP_DELAY_BLOCKS * 0.25  # Arbitrum 0.25s block time

        for i in range(cfg.TWAP_SPLITS):
            slice_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            if self.passive:
                # Shadow mode: log without spending real ETH
                if shadow_callback:
                    await shadow_callback(asset, side, slice_usd)
                slices.append(TWAPSlice(
                    index=i + 1, total_slices=cfg.TWAP_SPLITS,
                    amount_usd=round(slice_usd, 2), rpc_used=best.url,
                    timestamp=slice_ts, tx_hash=None, status="SHADOW",
                ))
                logger.info(
                    "[SHADOW] TWAP %d/%d: %s %s $%.2f",
                    i + 1, cfg.TWAP_SPLITS, side, asset, slice_usd,
                )
            else:
                # Active mode: submit via Flashbots
                tx_hash = await self._submit_transaction(
                    asset=asset, side=side, amount_usd=slice_usd,
                    rpc_url=cfg.FLASHBOTS_RPC,
                )
                status = "EXECUTED" if tx_hash else "FAILED"
                slices.append(TWAPSlice(
                    index=i + 1, total_slices=cfg.TWAP_SPLITS,
                    amount_usd=round(slice_usd, 2), rpc_used=cfg.FLASHBOTS_RPC,
                    timestamp=slice_ts, tx_hash=tx_hash, status=status,
                ))
                if tx_hash:
                    logger.info(
                        "TWAP %d/%d: %s %s $%.2f tx=%s",
                        i + 1, cfg.TWAP_SPLITS, side, asset, slice_usd, tx_hash,
                    )
                else:
                    logger.error("TWAP %d/%d FAILED for %s", i + 1, cfg.TWAP_SPLITS, asset)

            # Delay between slices (except after last)
            if i < cfg.TWAP_SPLITS - 1:
                await asyncio.sleep(delay_s)

        return ExecutionResult(
            asset=asset, side=side, total_usd=total_usd,
            slices=slices, rpc_node=best,
            is_passive=self.passive, timestamp=ts,
        )

    async def _submit_transaction(
        self,
        asset: str,
        side: str,
        amount_usd: float,
        rpc_url: str,
    ) -> str | None:
        """
        Sign and submit a transaction via Flashbots Protect RPC.
        Returns tx hash on success, None on failure.
        """
        if not cfg.ARB_PRIVATE_KEY:
            logger.error("No private key configured — cannot submit transaction")
            return None

        try:
            from web3 import AsyncWeb3, AsyncHTTPProvider

            w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
            account = w3.eth.account.from_key(cfg.ARB_PRIVATE_KEY)

            async with self._nonce_lock:
                nonce = await w3.eth.get_transaction_count(account.address)

            gas_price = await w3.eth.gas_price
            gas_estimate = 200_000  # conservative for swap

            tx = {
                "from": account.address,
                "to": account.address,  # placeholder — real impl routes via DEX
                "value": 0,
                "gas": int(gas_estimate * cfg.GAS_SAFETY_MARGIN),
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": 42161,  # Arbitrum One
            }

            signed = w3.eth.account.sign_transaction(tx, cfg.ARB_PRIVATE_KEY)
            tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
            return tx_hash.hex()

        except Exception as exc:
            logger.error("Transaction submission failed: %s", exc)
            return None

    # ── Background RPC ping loop ──────────────────────────────────────────────

    async def rpc_ping_loop(self, stop_event: asyncio.Event | None = None) -> None:
        """Background task: re-ping RPCs every RPC_PING_INTERVAL_S."""
        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                nodes = await self.ping_all_rpcs()
                alive = [n for n in nodes if n.alive]
                logger.debug(
                    "RPC ping: %d/%d alive, best=%s (%.0fms)",
                    len(alive), len(nodes),
                    self._best_rpc.url if self._best_rpc else "none",
                    self._best_rpc.latency_ms if self._best_rpc else 0,
                )
            except Exception as exc:
                logger.warning("RPC ping loop error: %s", exc)
            await asyncio.sleep(cfg.RPC_PING_INTERVAL_S)


# ── Standalone test ───────────────────────────────────────────────────────────

async def _test_executor() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    ex = Executor(passive=True)
    try:
        print(f"\n{'='*60}")
        print("  EXECUTOR v6.0 — RPC Rotation & TWAP Test")
        print(f"{'='*60}")

        print("\n[1/2] Pinging all RPCs...")
        nodes = await ex.ping_all_rpcs()
        for n in nodes:
            icon = "OK" if n.alive else "FAIL"
            blk = f"block {n.block_number:,}" if n.alive else "unreachable"
            print(f"      [{icon}] {n.url[:50]}: {n.latency_ms:.0f}ms — {blk}")

        best = await ex.get_best_rpc()
        if best:
            print(f"\n      Best: {best.url[:50]} ({best.latency_ms:.0f}ms)")

        print(f"\n[2/2] Shadow TWAP execution (ARB BUY $150)...")
        result = await ex.execute_twap(asset="ARB", side="BUY", total_usd=150.0)
        for s in result.slices:
            print(f"      [{s.status}] Slice {s.index}/{s.total_slices}: ${s.amount_usd:.2f}")

        print(f"\n{'='*60}\n")

    finally:
        await ex.close()


if __name__ == "__main__":
    asyncio.run(_test_executor())

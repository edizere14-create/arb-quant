"""
alpha_engine.py  (v6.0 — God-Tier Edition)
Async alpha signal generation combining three sub-signals into a God-Signal.

Sub-signals:
    A. Lead-Lag Correlation — 60s rolling window, Pyth primary with Chainlink cross-check
  B. Bridge Flow Z-Score  — wraps bridge_signal.py (dual-source validated)
  C. Sentiment Engine     — weighted score from configurable API

God-Signal fires ONLY when:
    Default mode: (Consensus > 0.85) AND (Bridge Z > 1.5) AND (Sentiment > 0.4)
    Test mode:    (Consensus > 0.6)  AND (Bridge Z > 0.5) AND (Sentiment > 0.1)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
import numpy as np

# ── Existing module imports (wrap & import) ───────────────────────────────────
from bridge_signal import run_bridge_signal
from hurst_regime import hurst_exponent, classify_regime, rv_zscore as compute_rv_z

import config as cfg

logger = logging.getLogger("alpha_engine")


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PriceTick:
    pair: str
    price: float
    timestamp: float
    source: str  # "pyth" | "chainlink"


@dataclass(frozen=True)
class LeadLagResult:
    correlation_matrix: dict[str, dict[str, float]]
    optimal_lags: dict[str, int]
    consensus_score: float
    oracle_halted: bool
    divergence_details: dict[str, float]
    sample_counts: dict[str, int]
    confirmed_pairs: dict[str, bool]
    status: str
    buffer_size: int
    required_buffer_size: int
    has_invalid_pair: bool
    timestamp: str


@dataclass(frozen=True)
class BridgeResult:
    z_bridge: float
    z_stable: float
    dual_confirmed: bool
    corr_valid: bool
    entry_signal: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class SentimentResult:
    score: float       # -1 to +1
    social: float
    fear_greed: float
    whale_activity: float
    source: str
    is_fallback: bool


@dataclass(frozen=True)
class GodSignal:
    fires: bool
    consensus_score: float
    lead_lag_score: float
    bridge_z: float
    sentiment_score: float
    lead_lag: LeadLagResult
    bridge: BridgeResult
    sentiment: SentimentResult
    regime: str
    hurst: float
    timestamp: str
    reason: str


@dataclass(frozen=True)
class AlphaDecayResult:
    """Result of alpha decay / reversal check for active positions."""
    emergency_exit: bool
    eth_1m_return: float
    bridge_z: float
    reason: str


# ── Sentiment Provider Protocol ───────────────────────────────────────────────

class SentimentProvider(Protocol):
    """Pluggable interface — swap in LunarCrush, Santiment, etc."""
    async def fetch(self, client: httpx.AsyncClient) -> SentimentResult: ...


# ── Pyth Hermes Client (Pull Model) ──────────────────────────────────────────

class PythHermesClient:
    """
    Fetches latest prices from Pyth's Hermes REST API.
    Unlike Chainlink (push), Pyth requires proactive pulls.
    Endpoint: GET /v2/updates/price/latest?ids[]=<feed_id>
    """

    def __init__(self, base_url: str = cfg.PYTH_HERMES_URL):
        self.base_url = base_url.rstrip("/")

    async def get_latest_prices(
        self, pairs: list[str], client: httpx.AsyncClient,
    ) -> dict[str, float]:
        """Pull latest prices for multiple pairs in one request."""
        feed_ids = [cfg.PYTH_FEED_IDS[p] for p in pairs if p in cfg.PYTH_FEED_IDS]
        if not feed_ids:
            return {}

        params: list[tuple[str, str | int | float | bool | None]] = [("ids[]", fid) for fid in feed_ids]
        try:
            params.append(("ignore_invalid_price_ids", "true"))
            resp = await client.get(
                f"{self.base_url}/v2/updates/price/latest",
                params=tuple(params),
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("Pyth Hermes fetch failed: %s", exc)
            return {}

        prices: dict[str, float] = {}
        for entry in data.get("parsed", []):
            feed_id = entry.get("id", "")
            price_data = entry.get("price", {})
            price_val = int(price_data.get("price", 0))
            expo = int(price_data.get("expo", 0))
            if price_val == 0:
                continue
            real_price = price_val * (10 ** expo)
            for pair, fid in cfg.PYTH_FEED_IDS.items():
                if fid == feed_id and pair in pairs:
                    prices[pair] = real_price
                    break

        return prices


# ── Chainlink Client (on-chain cross-check) ──────────────────────────────────

class ChainlinkClient:
    """
    Reads latest price from Chainlink AggregatorV3 on Arbitrum.
    Used as safety cross-check against Pyth — not primary.
    """

    def __init__(self, rpc_url: str = cfg.ARB_RPC_URL):
        self.rpc_url = rpc_url
        self._w3: Any = None

    async def _get_w3(self) -> Any:
        if self._w3 is None:
            from web3 import AsyncWeb3, AsyncHTTPProvider
            self._w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc_url))
        return self._w3

    async def get_latest_price(self, pair: str) -> float | None:
        address = cfg.CHAINLINK_FEEDS.get(pair)
        if not address:
            return None
        try:
            w3 = await self._get_w3()
            contract = w3.eth.contract(
                address=w3.to_checksum_address(address), abi=cfg.CHAINLINK_ABI,
            )
            round_data = await contract.functions.latestRoundData().call()
            decimals = await contract.functions.decimals().call()
            answer = int(round_data[1])  # int256 answer
            return float(answer) / float(10 ** int(decimals))
        except Exception as exc:
            logger.warning("Chainlink read failed for %s: %s", pair, exc)
            return None


# ── Sentiment Providers ───────────────────────────────────────────────────────

class MockSentimentProvider:
    """Returns neutral sentiment when no real API is configured."""

    async def fetch(self, client: httpx.AsyncClient) -> SentimentResult:
        return SentimentResult(
            score=0.0, social=0.0, fear_greed=0.5,
            whale_activity=0.0, source="mock", is_fallback=True,
        )


class APISentimentProvider:
    """
    Fetches from a configurable HTTP endpoint.
    Expected JSON: {"social": float, "fear_greed": float, "whale_activity": float}
    Weights: 0.4 * social + 0.3 * fear_greed_norm + 0.3 * whale_activity
    """

    def __init__(self, api_url: str):
        self.api_url = api_url

    async def fetch(self, client: httpx.AsyncClient) -> SentimentResult:
        try:
            resp = await client.get(self.api_url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()

            social = max(-1.0, min(1.0, float(data.get("social", 0.0))))
            fg_raw = float(data.get("fear_greed", 50.0))
            fear_greed = max(-1.0, min(1.0, (fg_raw - 50.0) / 50.0))
            whale = max(-1.0, min(1.0, float(data.get("whale_activity", 0.0))))

            score = max(-1.0, min(1.0, 0.4 * social + 0.3 * fear_greed + 0.3 * whale))

            return SentimentResult(
                score=score, social=social, fear_greed=fear_greed,
                whale_activity=whale, source=self.api_url, is_fallback=False,
            )
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("Sentiment API failed (%s): %s", self.api_url, exc)
            return SentimentResult(
                score=0.0, social=0.0, fear_greed=0.0,
                whale_activity=0.0, source="fallback", is_fallback=True,
            )


# ── Alpha Engine ──────────────────────────────────────────────────────────────

class AlphaEngine:
    """
    Async alpha engine combining Lead-Lag, Bridge Z-Score, and Sentiment
    into a single 'God-Signal' consensus.
    """

    def __init__(
        self,
        rpc_url: str = cfg.ARB_RPC_URL,
        sentiment_api_url: str = cfg.SENTIMENT_API_URL,
        pyth_url: str = cfg.PYTH_HERMES_URL,
    ):
        self.pyth = PythHermesClient(base_url=pyth_url)
        self.chainlink = ChainlinkClient(rpc_url=rpc_url)

        if sentiment_api_url:
            self.sentiment_provider: SentimentProvider = APISentimentProvider(sentiment_api_url)
        else:
            self.sentiment_provider = MockSentimentProvider()

        self.required_buffer_size = 60
        self.required_window_seconds = max(
            cfg.ROLLING_WINDOW_SECONDS,
            int(self.required_buffer_size * cfg.LEAD_LAG_SAMPLE_INTERVAL_S),
        )

        self.windows: dict[str, deque[PriceTick]] = {
            pair: deque(maxlen=256)
            for pair in cfg.LEADERS + cfg.FOLLOWERS
        }

        self._http_client: httpx.AsyncClient | None = None

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

    # ── Sub-signal A: Lead-Lag Correlation ────────────────────────────────────

    async def _fetch_and_update_prices(self) -> dict[str, float]:
        """Pull latest prices from Pyth, update rolling windows."""
        client = await self._get_client()
        all_pairs = cfg.LEADERS + cfg.FOLLOWERS
        now = time.time()

        pyth_prices = await self.pyth.get_latest_prices(all_pairs, client)
        for pair, price in pyth_prices.items():
            self.windows[pair].append(PriceTick(pair=pair, price=price, timestamp=now, source="pyth"))

        self._prune_windows(now)

        return pyth_prices

    def _prune_windows(self, now: float) -> None:
        min_timestamp = now - self.required_window_seconds
        for window in self.windows.values():
            while window and window[0].timestamp < min_timestamp:
                window.popleft()

    def _compute_returns(self, pair: str) -> np.ndarray:
        window = self.windows[pair]
        if len(window) < 3:
            return np.array([])
        prices = np.asarray([tick.price for tick in window], dtype=np.float64)
        return np.diff(np.log(prices))

    async def _fetch_chainlink_prices(self, pairs: list[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for pair in pairs:
            result = await self.chainlink.get_latest_price(pair)
            if result is None:
                continue
            result_value = float(result)
            if result_value <= 0:
                continue
            prices[pair] = result_value
        return prices

    async def get_lead_lag_consensus(self) -> LeadLagResult:
        """
        Pearson correlation across rolling windows.
        Tests leader returns vs lagged follower returns (lag 1-5).
        Returns consensus 0 (no edge) to 1 (strong lag detected).
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        required_buffer_size = self.required_buffer_size
        pyth_prices = await self._fetch_and_update_prices()
        tracked_pairs = sorted({pair for pair_tuple in cfg.LEAD_LAG_PAIRS for pair in pair_tuple})
        chainlink_prices = await self._fetch_chainlink_prices(tracked_pairs)
        window_lengths = {pair: len(self.windows[pair]) for pair in tracked_pairs}
        min_buffer_size = min(window_lengths.values(), default=0)
        logger.info(
            "Lead-lag buffer: %d/%d samples (%s)",
            min_buffer_size,
            required_buffer_size,
            ", ".join(f"{pair}={window_lengths[pair]}" for pair in tracked_pairs),
        )

        # Oracle divergence cross-check (Pyth vs Chainlink)
        divergence: dict[str, float] = {}
        asset_confirmation: dict[str, bool] = {}
        for pair in tracked_pairs:
            pyth_p = pyth_prices.get(pair)
            cl_p = chainlink_prices.get(pair)
            if pyth_p is None:
                continue
            if cl_p is None:
                asset_confirmation[pair] = False
                continue
            div = abs(pyth_p - cl_p) / cl_p
            divergence[pair] = round(div, 6)
            asset_confirmation[pair] = div <= cfg.ORACLE_DIVERGENCE
            if div > cfg.ORACLE_DIVERGENCE:
                logger.warning(
                    "Oracle divergence HALT: %s Pyth=%.4f CL=%.4f div=%.4f",
                    pair, pyth_p, cl_p, div,
                )
                return LeadLagResult(
                    correlation_matrix={},
                    optimal_lags={},
                    consensus_score=0.0,
                    oracle_halted=True,
                    divergence_details=divergence,
                    sample_counts=window_lengths,
                    confirmed_pairs={},
                    status="Oracle Halted",
                    buffer_size=min_buffer_size,
                    required_buffer_size=required_buffer_size,
                    has_invalid_pair=False,
                    timestamp=ts,
                )

        if min_buffer_size < required_buffer_size:
            logger.info(
                "Lead-lag warming up: buffer=%d/%d",
                min_buffer_size,
                required_buffer_size,
            )
            result = LeadLagResult(
                correlation_matrix={},
                optimal_lags={},
                consensus_score=0.0,
                oracle_halted=False,
                divergence_details=divergence,
                sample_counts=window_lengths,
                confirmed_pairs={},
                status="Warming Up",
                buffer_size=min_buffer_size,
                required_buffer_size=required_buffer_size,
                has_invalid_pair=False,
                timestamp=ts,
            )
            (cfg.CACHE_DIR / "lead_lag.json").write_text(
                json.dumps(asdict(result), indent=2, default=str)
            )
            return result

        # Correlation matrix: (leader, follower) at each lag
        corr_matrix: dict[str, dict[str, float]] = {}
        optimal_lags: dict[str, int] = {}
        sample_counts: dict[str, int] = {}
        confirmed_pairs: dict[str, bool] = {}
        pair_edges: list[float] = []
        has_invalid_pair = False

        for leader, follower in cfg.LEAD_LAG_PAIRS:
            leader_ret = self._compute_returns(leader)
            follower_ret = self._compute_returns(follower)
            min_len = min(len(leader_ret), len(follower_ret))
            pair_key = f"{leader}->{follower}"
            if leader not in corr_matrix:
                corr_matrix[leader] = {}
            confirmed_pairs[pair_key] = asset_confirmation.get(leader, True) and asset_confirmation.get(follower, True)

            if min_len < max(cfg.MIN_LEAD_LAG_POINTS, cfg.MAX_LAG + 2):
                corr_matrix[leader][follower] = 0.0
                optimal_lags[pair_key] = 0
                sample_counts[pair_key] = min_len
                continue

            best_corr = 0.0
            best_lag = 0
            best_samples = 0
            zero_variance_detected = False
            for lag in range(1, cfg.MAX_LAG + 1):
                if min_len <= lag + 2:
                    break
                x = leader_ret[: min_len - lag]
                y = follower_ret[lag: min_len]
                n = min(len(x), len(y))
                if n < cfg.MIN_LEAD_LAG_POINTS:
                    continue
                x, y = x[:n], y[:n]
                lead_std = float(np.std(x))
                lag_std = float(np.std(y))
                if lead_std == 0.0 or lag_std == 0.0:
                    zero_variance_detected = True
                    has_invalid_pair = True
                    best_corr = 0.0
                    best_lag = 0
                    best_samples = n
                    logger.info(
                        "Lead-lag zero variance: %s lead_std=%.6f lag_std=%.6f samples=%d",
                        pair_key,
                        lead_std,
                        lag_std,
                        n,
                    )
                    break
                r = float(np.corrcoef(x, y)[0, 1])
                if r > best_corr:
                    best_corr = r
                    best_lag = lag
                    best_samples = n

            if zero_variance_detected:
                corr_matrix[leader][follower] = 0.0
                optimal_lags[pair_key] = 0
                sample_counts[pair_key] = best_samples or min_len
                pair_edges.append(0.0)
                continue

            corr_matrix[leader][follower] = round(best_corr, 6)
            optimal_lags[pair_key] = best_lag
            sample_counts[pair_key] = best_samples or min_len
            pair_edges.append(max(0.0, best_corr))

        consensus = float(np.mean(pair_edges)) if pair_edges else 0.0
        consensus = max(0.0, min(1.0, consensus))

        result = LeadLagResult(
            correlation_matrix=corr_matrix,
            optimal_lags=optimal_lags,
            consensus_score=round(consensus, 6),
            oracle_halted=False,
            divergence_details=divergence,
            sample_counts=sample_counts,
            confirmed_pairs=confirmed_pairs,
            status="Zero Variance Guard" if has_invalid_pair else "Ready",
            buffer_size=min_buffer_size,
            required_buffer_size=required_buffer_size,
            has_invalid_pair=has_invalid_pair,
            timestamp=ts,
        )
        (cfg.CACHE_DIR / "lead_lag.json").write_text(
            json.dumps(asdict(result), indent=2, default=str)
        )
        return result

    # ── Sub-signal B: Bridge Flow Z-Score ─────────────────────────────────────

    async def get_bridge_signal(self) -> BridgeResult:
        """Wrap existing bridge_signal.run_bridge_signal() via asyncio.to_thread."""
        raw = await asyncio.to_thread(run_bridge_signal, verbose=False)
        return BridgeResult(
            z_bridge=raw.get("z_bridge", 0.0),
            z_stable=raw.get("z_stable", 0.0),
            dual_confirmed=raw.get("dual_confirmed", False),
            corr_valid=raw.get("corr_valid", False),
            entry_signal=raw.get("entry_signal", False),
            raw=raw,
        )

    # ── Sub-signal C: Sentiment Engine ────────────────────────────────────────

    async def get_sentiment(self) -> SentimentResult:
        client = await self._get_client()
        return await self.sentiment_provider.fetch(client)

    # ── God-Signal Consensus ──────────────────────────────────────────────────

    async def compute_god_signal(self) -> GodSignal:
        """
        Combine all three sub-signals.
        Fires ONLY if ALL three gates pass simultaneously:
          1. Consensus Score > configured threshold
          2. Bridge Z-Score > configured threshold
          3. Sentiment > configured threshold
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        consensus_threshold = (
            cfg.TEST_CONSENSUS_THRESHOLD if cfg.TEST_MODE else cfg.CONSENSUS_THRESHOLD
        )
        bridge_threshold = (
            cfg.TEST_BRIDGE_Z_THRESHOLD if cfg.TEST_MODE else cfg.BRIDGE_Z_THRESHOLD
        )
        sentiment_threshold = (
            cfg.TEST_SENTIMENT_THRESHOLD if cfg.TEST_MODE else cfg.SENTIMENT_THRESHOLD
        )

        lead_lag, bridge, sentiment = await asyncio.gather(
            self.get_lead_lag_consensus(),
            self.get_bridge_signal(),
            self.get_sentiment(),
        )

        lead_lag_score = lead_lag.consensus_score
        bridge_z = bridge.z_bridge
        sentiment_score = sentiment.score
        shadow_test_override = False

        # Oracle halt overrides everything
        if lead_lag.oracle_halted:
            signal = GodSignal(
                fires=False, consensus_score=0.0,
                lead_lag_score=0.0, bridge_z=bridge.z_bridge,
                sentiment_score=sentiment.score,
                lead_lag=lead_lag, bridge=bridge, sentiment=sentiment,
                regime="UNKNOWN", hurst=0.0, timestamp=ts,
                reason="ORACLE DIVERGENCE HALT — Pyth/Chainlink diverged > 2%",
            )
            self._cache_god_signal(signal)
            return signal

        if (
            cfg.TEST_MODE
            and cfg.is_passive_mode()
            and cfg.TEST_SHADOW_FORCE_FLOORS
            and lead_lag.status == "Ready"
            and not lead_lag.has_invalid_pair
        ):
            lead_lag_score = max(lead_lag_score, cfg.TEST_SHADOW_LEAD_LAG_FLOOR)
            bridge_z = max(bridge_z, cfg.TEST_SHADOW_BRIDGE_Z_FLOOR)
            sentiment_score = max(sentiment_score, cfg.TEST_SHADOW_SENTIMENT_FLOOR)
            shadow_test_override = True

        # Hurst regime context
        hurst_val: float = 0.5
        regime: str = "Random Walk"
        try:
            hurst_data = await asyncio.to_thread(self._compute_hurst_sync)
            hurst_val = float(hurst_data.get("hurst", 0.5))
            regime = str(hurst_data.get("regime", "Random Walk"))
        except Exception:
            pass

        # Normalize for weighted consensus
        bridge_z_normalized = max(0.0, min(1.0, bridge_z / 3.0))
        sentiment_normalized = (sentiment_score + 1.0) / 2.0

        consensus = (
            cfg.W_LEAD_LAG * lead_lag_score
            + cfg.W_BRIDGE * bridge_z_normalized
            + cfg.W_SENTIMENT * sentiment_normalized
        )
        consensus = round(max(0.0, min(1.0, consensus)), 6)

        # Triple-gate AND
        gate_consensus = consensus > consensus_threshold
        gate_bridge = bridge_z > bridge_threshold
        gate_sentiment = sentiment_score > sentiment_threshold

        fires = gate_consensus and gate_bridge and gate_sentiment

        if fires:
            mode_label = "TEST MODE" if cfg.TEST_MODE else "LIVE MODE"
            if shadow_test_override:
                reason = f"GOD-SIGNAL FIRES — shadow test floors applied ({mode_label})"
            else:
                reason = f"GOD-SIGNAL FIRES — all gates passed ({mode_label})"
        else:
            failures = []
            if lead_lag.status != "Ready":
                failures.append(f"lead_lag {lead_lag.status.lower()} ({lead_lag.buffer_size}/{lead_lag.required_buffer_size})")
            elif lead_lag.has_invalid_pair:
                failures.append("lead_lag zero variance guarded")
            if not gate_consensus:
                failures.append(f"consensus {consensus:.4f} <= {consensus_threshold}")
            if not gate_bridge:
                failures.append(f"bridge_z {bridge_z:.4f} <= {bridge_threshold}")
            if not gate_sentiment:
                failures.append(f"sentiment {sentiment_score:.4f} <= {sentiment_threshold}")
            reason = "NO FIRE — " + " | ".join(failures)

        signal = GodSignal(
            fires=fires, consensus_score=consensus,
            lead_lag_score=lead_lag_score,
            bridge_z=bridge_z,
            sentiment_score=sentiment_score,
            lead_lag=lead_lag, bridge=bridge, sentiment=sentiment,
            regime=regime, hurst=round(hurst_val, 4),
            timestamp=ts, reason=reason,
        )

        self._cache_god_signal(signal)
        logger.info(
            "God-Signal: fires=%s test_mode=%s shadow_override=%s consensus=%.4f bridge_z=%.4f sentiment=%.4f regime=%s",
            fires, cfg.TEST_MODE, shadow_test_override, consensus, bridge_z, sentiment_score, regime,
        )
        return signal

    # ── Alpha Decay Exit Check ────────────────────────────────────────────

    def check_alpha_decay(self, asset: str, bridge_z: float) -> AlphaDecayResult:
        """
        Check for alpha reversal that should trigger an emergency exit.
        Reversal trigger: ETH 1-min return < -0.5% OR bridge_z < 0.0.
        """
        eth_pair = "ETH/USD"
        eth_return = 0.0
        window = self.windows.get(eth_pair)
        if window and len(window) >= 2:
            # Compute 1-minute return: compare latest price to price ~60s ago
            latest = window[-1]
            oldest_candidate = window[0]
            for tick in window:
                if latest.timestamp - tick.timestamp >= 60.0:
                    oldest_candidate = tick
                    break
            if oldest_candidate.price > 0:
                eth_return = (latest.price - oldest_candidate.price) / oldest_candidate.price

        reasons: list[str] = []
        if eth_return < -0.005:
            reasons.append(f"ETH 1m return {eth_return:.4%} < -0.50%")
        if bridge_z < 0.0:
            reasons.append(f"Bridge Z {bridge_z:.4f} < 0.0")

        emergency = len(reasons) > 0
        reason = " | ".join(reasons) if reasons else "Alpha intact"

        if emergency:
            logger.warning("ALPHA DECAY detected for %s: %s", asset, reason)

        return AlphaDecayResult(
            emergency_exit=emergency,
            eth_1m_return=round(eth_return, 6),
            bridge_z=round(bridge_z, 4),
            reason=reason,
        )

    async def get_latest_asset_price(self, pair: str) -> float | None:
        """Fetch the latest price for a single pair from Pyth.  Returns None on failure."""
        client = await self._get_client()
        prices = await self.pyth.get_latest_prices([pair], client)
        return prices.get(pair)

    @staticmethod
    def _compute_hurst_sync() -> dict[str, Any]:
        """Synchronous Hurst computation — called via asyncio.to_thread."""
        import requests as _requests
        try:
            resp = _requests.get(
                "https://coins.llama.fi/chart/coingecko:ethereum?span=60&period=1d",
                timeout=12, headers={"User-Agent": "arb-quant/6.0"},
            )
            resp.raise_for_status()
            data = resp.json()
            prices = np.array(
                [p["price"] for p in data["coins"]["coingecko:ethereum"]["prices"]]
            )
            log_ret = np.diff(np.log(prices))
            if len(log_ret) < 10:
                return {"hurst": 0.5, "regime": "Random Walk", "rv_z": 0.0}

            h = float(hurst_exponent(log_ret[-30:]))
            rv_z = float(compute_rv_z(log_ret))
            regime = classify_regime(h, rv_z)
            return {"hurst": h, "regime": regime, "rv_z": rv_z}
        except Exception:
            return {"hurst": 0.5, "regime": "Random Walk", "rv_z": 0.0}

    def _cache_god_signal(self, signal: GodSignal) -> None:
        out = {
            "fires": signal.fires,
            "consensus_score": signal.consensus_score,
            "lead_lag_score": signal.lead_lag_score,
            "bridge_z": signal.bridge_z,
            "sentiment_score": signal.sentiment_score,
            "regime": signal.regime,
            "hurst": signal.hurst,
            "timestamp": signal.timestamp,
            "reason": signal.reason,
            "lead_lag": asdict(signal.lead_lag),
            "bridge": {
                "z_bridge": signal.bridge.z_bridge,
                "z_stable": signal.bridge.z_stable,
                "dual_confirmed": signal.bridge.dual_confirmed,
                "corr_valid": signal.bridge.corr_valid,
                "entry_signal": signal.bridge.entry_signal,
            },
            "sentiment": asdict(signal.sentiment),
        }
        (cfg.CACHE_DIR / "god_signal.json").write_text(
            json.dumps(out, indent=2, default=str)
        )


# ── Standalone test ───────────────────────────────────────────────────────────

async def _test_engine() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    engine = AlphaEngine()
    try:
        print(f"\n{'='*60}")
        print("  ALPHA ENGINE v6.0 — God-Signal Test")
        print(f"{'='*60}")

        print("\n[1/3] Lead-Lag Correlation (Pyth + Chainlink)...")
        ll = await engine.get_lead_lag_consensus()
        print(f"      Consensus: {ll.consensus_score:.4f}")
        print(f"      Oracle halted: {ll.oracle_halted}")
        print(f"      Status: {ll.status} ({ll.buffer_size}/{ll.required_buffer_size})")
        if ll.correlation_matrix:
            for leader, followers in ll.correlation_matrix.items():
                for follower, corr in followers.items():
                    lag = ll.optimal_lags.get(f"{leader}->{follower}", 0)
                    print(f"      {leader} -> {follower}: r={corr:+.4f} lag={lag}")
        else:
            print("      (insufficient data — need 60+ price points)")

        print("\n[2/3] Bridge Flow Z-Score...")
        br = await engine.get_bridge_signal()
        print(f"      z_bridge={br.z_bridge:+.4f} z_stable={br.z_stable:+.4f}")
        print(f"      dual_confirmed={br.dual_confirmed} corr_valid={br.corr_valid}")

        print("\n[3/3] Sentiment Engine...")
        sent = await engine.get_sentiment()
        print(f"      score={sent.score:+.4f} source={sent.source} fallback={sent.is_fallback}")

        print(f"\n{'─'*60}")
        print("  GOD-SIGNAL COMPUTATION")
        print(f"{'─'*60}")
        god = await engine.compute_god_signal()
        consensus_threshold = cfg.TEST_CONSENSUS_THRESHOLD if cfg.TEST_MODE else cfg.CONSENSUS_THRESHOLD
        bridge_threshold = cfg.TEST_BRIDGE_Z_THRESHOLD if cfg.TEST_MODE else cfg.BRIDGE_Z_THRESHOLD
        sentiment_threshold = cfg.TEST_SENTIMENT_THRESHOLD if cfg.TEST_MODE else cfg.SENTIMENT_THRESHOLD
        print(f"  Consensus:  {god.consensus_score:.4f}  (threshold: {consensus_threshold})")
        print(f"  Bridge Z:   {god.bridge_z:+.4f}  (threshold: {bridge_threshold})")
        print(f"  Sentiment:  {god.sentiment_score:+.4f}  (threshold: {sentiment_threshold})")
        print(f"  Regime:     {god.regime} (H={god.hurst:.4f})")
        icon = "FIRE" if god.fires else "HOLD"
        print(f"\n  [{icon}] {god.reason}")
        print(f"{'='*60}\n")

    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(_test_engine())

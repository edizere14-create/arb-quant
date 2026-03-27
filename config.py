"""
config.py — Arbitrum Alpha Operator v6.0
Centralized configuration loaded from .env via python-dotenv.
All modules import from here instead of hardcoding values.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ── Load .env ─────────────────────────────────────────────────────────────────
_ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_FILE)


# ── RPC ───────────────────────────────────────────────────────────────────────
ARB_RPC_URL: str      = os.getenv("ARB_RPC_URL", "https://arb1.arbitrum.io/rpc")
ARB_RPC_BACKUP_1: str = os.getenv("ARB_RPC_BACKUP_1", "https://arbitrum.llamarpc.com")
ARB_RPC_BACKUP_2: str = os.getenv("ARB_RPC_BACKUP_2", "https://rpc.ankr.com/arbitrum")
RPC_ENDPOINTS: list[str] = [ARB_RPC_URL, ARB_RPC_BACKUP_1, ARB_RPC_BACKUP_2]

FLASHBOTS_RPC: str = "https://rpc.flashbots.net/fast"
MAX_RPC_LATENCY_MS: int = 150


# ── Wallet ────────────────────────────────────────────────────────────────────
ARB_PRIVATE_KEY: str = os.getenv("ARB_PRIVATE_KEY", "")


# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN: str   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")


# ── Mode ──────────────────────────────────────────────────────────────────────
def is_passive_mode() -> bool:
    """Check PASSIVE_MODE env var. Can be overridden by --passive CLI flag."""
    env_val = os.getenv("PASSIVE_MODE", "true").lower()
    return env_val in ("true", "1", "yes")


TEST_MODE: bool = os.getenv("TEST_MODE", "false").lower() in ("true", "1", "yes")
STRATEGY_MODE: str = os.getenv("STRATEGY_MODE", "SNIPER").strip().upper() or "SNIPER"


# ── Portfolio ─────────────────────────────────────────────────────────────────
PORTFOLIO_USD: float     = float(os.getenv("PORTFOLIO_USD", "1000"))
MAX_PORTFOLIO_USD: float = 5_000.0
MAX_STRATEGY_PCT: float  = 0.30
KELLY_DAMPENER: float    = 0.5
MIN_POSITION_USD: float  = 25.0
PORTFOLIO_GROWTH_BASE_USD: float = float(os.getenv("PORTFOLIO_GROWTH_BASE_USD", str(PORTFOLIO_USD)))
PORTFOLIO_GROWTH_TARGET_USD: float = float(os.getenv("PORTFOLIO_GROWTH_TARGET_USD", str(MAX_PORTFOLIO_USD)))
GROWTH_RISK_FLOOR: float = float(os.getenv("GROWTH_RISK_FLOOR", "0.75"))
SNIPER_MAX_TRADE_PCT: float = float(os.getenv("SNIPER_MAX_TRADE_PCT", "0.30"))
MULTI_ASSET_MAX_TRADE_PCT: float = float(os.getenv("MULTI_ASSET_MAX_TRADE_PCT", "0.12"))
MULTI_ASSET_MAX_OPEN_POSITIONS: int = int(os.getenv("MULTI_ASSET_MAX_OPEN_POSITIONS", "3"))

# Per system prompt §0: max 30% per strategy, corr > 0.7 share one bucket
CORRELATION_BUCKET_THRESHOLD: float = 0.7
PORTFOLIO_CVAR_LIMIT: float         = 0.05  # 5%


# ── Chainlink Aggregator V3 Addresses (Arbitrum One) ─────────────────────────
CHAINLINK_FEEDS: dict[str, str] = {
    "ETH/USD":   "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612",
    "ARB/USD":   "0xb2A824043730FE05F3DA2efaFa1CBbe83fa548D6",
    "GMX/USD":   "0xDB98056FecFff59D032aB628337A4887110df3dB",
}

CHAINLINK_ABI: list[dict[str, object]] = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId",        "type": "uint80"},
            {"name": "answer",         "type": "int256"},
            {"name": "startedAt",      "type": "uint256"},
            {"name": "updatedAt",      "type": "uint256"},
            {"name": "answeredInRound","type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# ── Pyth Hermes (Pull-based Oracle) ──────────────────────────────────────────
PYTH_HERMES_URL: str = "https://hermes.pyth.network"

# Price Feed IDs — see https://pyth.network/developers/price-feed-ids
PYTH_FEED_IDS: dict[str, str] = {
    "ETH/USD":   "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "ARB/USD":   "3fa4252848f9f0a1480be62745a4629d9eb1322aebab8a791e344b3b9c1adcf5",
    "GMX/USD":   "b962539d0fcb272a494d65ea56f94851c2bcf8823935da05bd628916e2e9edbf",
    "RDNT/USD":  "c8cf45412be4268bef8f76a8b0d60971c6e57ab57919083b8e9f12ba72adeeb6",
    "PENDLE/USD": "9a4df90b25497f66b1afb012467e316e801ca3d839456db028892fe8c70c8016",
}


# ── Leader / Follower Sets ────────────────────────────────────────────────────
LEADERS: list[str]   = ["ETH/USD", "GMX/USD"]
FOLLOWERS: list[str] = ["ARB/USD", "RDNT/USD", "PENDLE/USD"]


# ── Signal Thresholds ────────────────────────────────────────────────────────
ROLLING_WINDOW_SECONDS: int = int(os.getenv("ROLLING_WINDOW_SECONDS", "60"))
MIN_LEAD_LAG_POINTS: int    = int(os.getenv("MIN_LEAD_LAG_POINTS", "10"))
LEAD_LAG_SAMPLE_INTERVAL_S: float = float(os.getenv("LEAD_LAG_SAMPLE_INTERVAL_S", "5"))
MAX_LAG: int                = 5       # max lag intervals to test
ORACLE_DIVERGENCE: float    = 0.02    # 2% → halt (system prompt §1)
BRIDGE_Z_THRESHOLD: float   = 1.5     # God-Signal gate
SENTIMENT_THRESHOLD: float  = 0.4     # God-Signal gate
CONSENSUS_THRESHOLD: float  = 0.85    # God-Signal gate

LEAD_LAG_PAIRS: list[tuple[str, str]] = [
    ("ETH/USD", "ARB/USD"),
    ("ETH/USD", "GMX/USD"),
    ("GMX/USD", "ARB/USD"),
]

TEST_BRIDGE_Z_THRESHOLD: float  = 0.5
TEST_SENTIMENT_THRESHOLD: float = 0.1
TEST_CONSENSUS_THRESHOLD: float = 0.6

# Shadow-only test floors to force end-to-end paper execution paths.
# These apply only when TEST_MODE=true and PASSIVE_MODE=true.
TEST_SHADOW_FORCE_FLOORS: bool = os.getenv("TEST_SHADOW_FORCE_FLOORS", "true").lower() in ("true", "1", "yes")
TEST_SHADOW_LEAD_LAG_FLOOR: float = 0.9
TEST_SHADOW_BRIDGE_Z_FLOOR: float = 1.0
TEST_SHADOW_SENTIMENT_FLOOR: float = 0.2

# Consensus weights
W_LEAD_LAG: float   = 0.4
W_BRIDGE: float     = 0.3
W_SENTIMENT: float  = 0.3


# ── Circuit Breaker ──────────────────────────────────────────────────────────
FLASH_CRASH_DEVIATION: float = 0.03   # 3% from 5-min EMA → halt
EMA_WINDOW_SECONDS: int      = 300    # 5 minutes

# Dead Man's Switch
HEARTBEAT_INTERVAL_S: float      = 2.5     # ~10 Arbitrum blocks (0.25s each)
MAX_CONSECUTIVE_HEARTBEAT_FAILS: int = 5
RETRY_DELAYS_S: list[float]      = [1.0, 4.0, 16.0]  # exponential backoff


# ── TWAP Execution ───────────────────────────────────────────────────────────
TWAP_SPLITS: int          = 3        # split order into 3 parts
TWAP_DELAY_BLOCKS: int    = 2        # ~0.5s between sub-orders
GAS_SAFETY_MARGIN: float  = 1.2      # gas_estimate * 1.2
RPC_PING_INTERVAL_S: int  = 60       # re-ping RPC every 60s


# ── Shadow Mirror ────────────────────────────────────────────────────────────
SHADOW_DB_PATH: str               = "shadow_trades.db"
SIMULATED_GAS_FEE_USD: float      = 0.02
SIMULATED_SLIPPAGE_PCT: float     = 0.001   # 0.1%


# ── Sentiment ────────────────────────────────────────────────────────────────
SENTIMENT_API_URL: str = os.getenv("SENTIMENT_API_URL", "")


# ── Cache ─────────────────────────────────────────────────────────────────────
CACHE_DIR: Path = Path(__file__).resolve().parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)


# ── Validation (non-fatal — modules can still import) ─────────────────────────
def validate_config(require_private_key: bool = False) -> list[str]:
    """Return list of missing / invalid config items. Empty = all good."""
    issues: list[str] = []
    if require_private_key and not ARB_PRIVATE_KEY:
        issues.append("ARB_PRIVATE_KEY is not set (required for active mode)")
    if not ARB_RPC_URL:
        issues.append("ARB_RPC_URL is not set")
    if PORTFOLIO_USD <= 0:
        issues.append(f"PORTFOLIO_USD must be positive (got {PORTFOLIO_USD})")
    if PORTFOLIO_USD > MAX_PORTFOLIO_USD:
        issues.append(f"PORTFOLIO_USD {PORTFOLIO_USD} exceeds cap {MAX_PORTFOLIO_USD}")
    if STRATEGY_MODE not in {"SNIPER", "MULTI_ASSET"}:
        issues.append(f"STRATEGY_MODE must be SNIPER or MULTI_ASSET (got {STRATEGY_MODE})")
    return issues


if __name__ == "__main__":
    issues = validate_config()
    if issues:
        print("Config issues:")
        for i in issues:
            print(f"  - {i}")
        sys.exit(1)
    print("Config OK")
    print(f"  RPC:       {ARB_RPC_URL}")
    print(f"  Backups:   {ARB_RPC_BACKUP_1}, {ARB_RPC_BACKUP_2}")
    print(f"  Portfolio: ${PORTFOLIO_USD:,.0f} (max ${MAX_PORTFOLIO_USD:,.0f})")
    print(f"  Passive:   {is_passive_mode()}")
    print(f"  Strategy:  {STRATEGY_MODE}")
    print(f"  Telegram:  {'configured' if TELEGRAM_TOKEN else 'not set'}")

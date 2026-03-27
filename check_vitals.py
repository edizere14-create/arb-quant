"""
check_vitals.py  (v6.0 — God-Tier Edition)
Pre-flight validation: pings RPCs, verifies Telegram, oracle feeds,
DefiLlama API, SQLite write access, and Python version.

Exit code 0 = all critical checks pass.
Exit code 1 = at least one critical check failed.

Usage:
  python check_vitals.py
"""

from __future__ import annotations

import asyncio
import json
import platform
import sqlite3
import sys
import time
from pathlib import Path

import httpx

# Import config — this also runs dotenv loading
import config as cfg


# ── Check registry ────────────────────────────────────────────────────────────
class CheckResult:
    __slots__ = ("name", "passed", "detail", "critical")

    def __init__(self, name: str, passed: bool, detail: str = "", critical: bool = True):
        self.name = name
        self.passed = passed
        self.detail = detail
        self.critical = critical

    @property
    def icon(self) -> str:
        return "✅" if self.passed else ("❌" if self.critical else "⚠️")


results: list[CheckResult] = []


# ── Individual checks ─────────────────────────────────────────────────────────

def check_python_version() -> CheckResult:
    v = sys.version_info
    passed = v >= (3, 11)
    detail = f"{v.major}.{v.minor}.{v.micro} ({platform.python_implementation()})"
    return CheckResult("Python ≥ 3.11", passed, detail)


def check_env_file() -> CheckResult:
    env_path = Path(__file__).resolve().parent / ".env"
    exists = env_path.exists()
    detail = str(env_path) if exists else "NOT FOUND"
    return CheckResult(".env file exists", exists, detail)


def check_config_validation() -> CheckResult:
    issues = cfg.validate_config(require_private_key=False)
    passed = len(issues) == 0
    detail = "; ".join(issues) if issues else "All config values valid"
    return CheckResult("Config validation", passed, detail)


async def check_rpc(url: str, label: str) -> CheckResult:
    async with httpx.AsyncClient() as client:
        start = time.monotonic()
        try:
            resp = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                timeout=10.0,
            )
            elapsed = (time.monotonic() - start) * 1000
            data = resp.json()
            block = int(data.get("result", "0x0"), 16)
            return CheckResult(
                f"RPC: {label}",
                True,
                f"block={block}  latency={elapsed:.0f}ms",
                critical=label == "Primary",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return CheckResult(
                f"RPC: {label}",
                False,
                f"{type(exc).__name__}: {exc}  ({elapsed:.0f}ms)",
                critical=label == "Primary",
            )


async def check_telegram() -> CheckResult:
    if not cfg.TELEGRAM_TOKEN:
        return CheckResult("Telegram bot", False, "TELEGRAM_TOKEN not set", critical=False)
    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/getMe"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, timeout=10.0)
            data = resp.json()
            if data.get("ok"):
                bot_name = data.get("result", {}).get("username", "unknown")
                chat_detail = f"chat_id={'set' if cfg.TELEGRAM_CHAT_ID else 'NOT SET'}"
                return CheckResult("Telegram bot", True, f"@{bot_name}  {chat_detail}", critical=False)
            return CheckResult("Telegram bot", False, f"API error: {data}", critical=False)
        except Exception as exc:
            return CheckResult("Telegram bot", False, str(exc), critical=False)


async def check_chainlink_feed(pair: str, address: str) -> CheckResult:
    """Try eth_call to latestRoundData() on the Chainlink aggregator."""
    # latestRoundData() selector = 0xfeaf968c
    data_hex = "0xfeaf968c"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                cfg.ARB_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_call",
                    "params": [{"to": address, "data": data_hex}, "latest"],
                },
                timeout=10.0,
            )
            result = resp.json().get("result", "")
            if result and len(result) > 66:
                # Parse answer (second 32-byte word), decimals assumed 8
                answer_hex = result[66:130]
                answer = int(answer_hex, 16)
                price = answer / 1e8
                return CheckResult(f"Chainlink {pair}", True, f"${price:,.4f}")
            return CheckResult(f"Chainlink {pair}", False, f"empty response: {result[:60]}")
        except Exception as exc:
            return CheckResult(f"Chainlink {pair}", False, str(exc))


async def check_pyth() -> CheckResult:
    """Fetch one price from Pyth Hermes to verify reachability."""
    feed_id = cfg.PYTH_FEED_IDS.get("ETH/USD", "")
    url = f"{cfg.PYTH_HERMES_URL}/v2/updates/price/latest?ids[]={feed_id}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, timeout=10.0)
            data = resp.json()
            parsed = data.get("parsed", [])
            if parsed:
                price_obj = parsed[0].get("price", {})
                price = int(price_obj.get("price", "0")) * 10 ** int(price_obj.get("expo", "0"))
                return CheckResult("Pyth Hermes (ETH)", True, f"${price:,.2f}")
            return CheckResult("Pyth Hermes (ETH)", False, "no parsed data")
        except Exception as exc:
            return CheckResult("Pyth Hermes (ETH)", False, str(exc))


async def check_defillama() -> CheckResult:
    url = "https://bridges.llama.fi/bridges?includeChains=true"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, timeout=10.0)
            data = resp.json()
            bridges = data.get("bridges", [])
            return CheckResult("DefiLlama API", True, f"{len(bridges)} bridges indexed")
        except Exception as exc:
            return CheckResult("DefiLlama API", False, str(exc), critical=False)


def check_sqlite() -> CheckResult:
    db_path = Path(cfg.SHADOW_DB_PATH)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS _vitals_test (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO _vitals_test (id) VALUES (1)")
        conn.execute("DELETE FROM _vitals_test WHERE id = 1")
        conn.execute("DROP TABLE _vitals_test")
        conn.commit()
        conn.close()
        return CheckResult("SQLite write", True, str(db_path))
    except Exception as exc:
        return CheckResult("SQLite write", False, str(exc))


def check_dependencies() -> CheckResult:
    missing = []
    for mod in ["httpx", "web3", "aiosqlite", "dotenv", "numpy", "pandas", "scipy", "streamlit"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return CheckResult("Dependencies", False, f"missing: {', '.join(missing)}")
    return CheckResult("Dependencies", True, "all installed")


# ── Runner ────────────────────────────────────────────────────────────────────

async def run_all_checks() -> list[CheckResult]:
    checks: list[CheckResult] = []

    # Sync checks
    checks.append(check_python_version())
    checks.append(check_env_file())
    checks.append(check_config_validation())
    checks.append(check_dependencies())
    checks.append(check_sqlite())

    # Async checks — run concurrently
    async_tasks = [
        check_rpc(cfg.ARB_RPC_URL, "Primary"),
        check_rpc(cfg.ARB_RPC_BACKUP_1, "Backup 1"),
        check_rpc(cfg.ARB_RPC_BACKUP_2, "Backup 2"),
        check_telegram(),
        check_pyth(),
        check_defillama(),
    ]

    # Chainlink feeds
    for pair, addr in cfg.CHAINLINK_FEEDS.items():
        async_tasks.append(check_chainlink_feed(pair, addr))

    async_results = await asyncio.gather(*async_tasks, return_exceptions=True)
    for r in async_results:
        if isinstance(r, CheckResult):
            checks.append(r)
        else:
            checks.append(CheckResult("Unknown", False, str(r)))

    return checks


def print_results(checks: list[CheckResult]) -> int:
    print()
    print("=" * 66)
    print("  Arbitrum Alpha Operator v6.0 — Pre-Flight Check")
    print("=" * 66)
    print()

    max_name = max(len(c.name) for c in checks) + 2
    critical_fail = False

    for c in checks:
        status = c.icon
        name = c.name.ljust(max_name)
        print(f"  {status}  {name}  {c.detail}")
        if not c.passed and c.critical:
            critical_fail = True

    print()
    passed = sum(1 for c in checks if c.passed)
    total = len(checks)
    print(f"  Result: {passed}/{total} checks passed", end="")
    if critical_fail:
        print("  ❌ CRITICAL FAILURES — do not start bot")
        print()
        return 1
    else:
        print("  ✅ All critical checks passed")
        print()
        return 0


def main() -> int:
    checks = asyncio.run(run_all_checks())
    return print_results(checks)


if __name__ == "__main__":
    sys.exit(main())

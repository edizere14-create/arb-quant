"""Smoke test: async get_dynamic_slippage from trade_executor."""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trade_executor import get_dynamic_slippage, estimate_v3_slippage

async def async_tests():
    lines = ["=== Async get_dynamic_slippage ==="]
    for size in [50, 100, 250, 500, 1000]:
        slip, method = await get_dynamic_slippage(size)
        lines.append(f"  ${size:>5}  ->  slippage = {slip:.4f}%  [{method}]")
    return lines

lines = asyncio.run(async_tests())

# Also test sync wrapper
lines.append("\n=== Sync estimate_v3_slippage ===")
slip, method = estimate_v3_slippage(100)
lines.append(f"  $  100  ->  slippage = {slip:.4f}%  [{method}]")

lines.append("\n=== All tests passed ===")
out = "\n".join(lines)
print(out)

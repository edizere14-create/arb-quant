"""Quick smoke test for V3 tick-based slippage."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_executor import estimate_v3_slippage

lines = ["=== V3 Tick-Based Slippage Test ==="]
for size in [50, 100, 250, 500, 1000]:
    slip, method = estimate_v3_slippage(size)
    lines.append(f"  ${size:>5}  ->  slippage = {slip:.4f}%  [{method}]")
lines.append("=== Done ===")
out = "\n".join(lines)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_slip_results.txt"), "w") as f:
    f.write(out)
print(out)

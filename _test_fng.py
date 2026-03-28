import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from monitor import fetch_fear_greed
r = fetch_fear_greed()
msg = f"value={r['value']} ({r['classification']}) -> {r['bias']}"
sys.stdout.write(msg + "\n")
sys.stdout.flush()

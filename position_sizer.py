"""
position_sizer.py
Kelly criterion sizing with volatility adjustment.
"""
import json
import numpy as np
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PORTFOLIO_USD    = 500.0
MAX_STRATEGY_PCT = 0.30
KELLY_DAMPENER   = 0.5
MIN_POSITION_USD = 25.0

SCORE_KELLY_MAP = [(80,0.25),(65,0.18),(50,0.10),(35,0.05),(0,0.0)]

def _get(url: str, timeout: int = 10) -> dict[str, Any] | None:
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "arb-quant/1.0"})
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def fetch_arb_volatility() -> float:
    try:
        data = _get("https://coins.llama.fi/chart/coingecko:arbitrum?span=45&period=1d")
        if not data:
            return 70.0
        coin_data = data.get("coins", {}) if isinstance(data, dict) else {}
        arb_data = coin_data.get("coingecko:arbitrum", {}) if isinstance(coin_data, dict) else {}
        price_rows = arb_data.get("prices", []) if isinstance(arb_data, dict) else []
        prices = np.asarray(
            [float(p["price"]) for p in price_rows if isinstance(p, dict) and "price" in p],
            dtype=np.float64,
        )
        if prices.size < 31:
            return 70.0
        log_returns = np.diff(np.log(prices))
        rolling_window = log_returns[-30:]
        if rolling_window.size == 0:
            return 70.0
        return float(np.std(rolling_window, ddof=1) * np.sqrt(365) * 100)
    except Exception:
        return 70.0

def kelly_from_score(score:int)->float:
    for t,f in SCORE_KELLY_MAP:
        if score>=t: return f
    return 0.0

def vol_adjustment(rv:float)->float:
    return round(min(max(70.0/max(rv,20.0),0.3),1.5),4)

def compute_position_size(signal_score:int,portfolio_usd:float=PORTFOLIO_USD,rv_override:float|None=None,verbose:bool=True)->dict[str, object]:
    rv=rv_override if rv_override is not None else fetch_arb_volatility()
    bk=kelly_from_score(signal_score)
    vf=vol_adjustment(rv)
    rf=bk*KELLY_DAMPENER*vf
    max_position_usd=portfolio_usd*MAX_STRATEGY_PCT
    pos=portfolio_usd*rf
    pos=max(MIN_POSITION_USD,min(pos,max_position_usd)) if bk>0 else 0.0
    result={"timestamp":datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "signal_score":signal_score,"portfolio_usd":portfolio_usd,
            "rv_30d_pct":round(rv,2),"base_kelly":bk,"kelly_dampener":KELLY_DAMPENER,
            "vol_adjustment":vf,"raw_fraction":round(rf,4),
            "position_usd":round(pos,2),"pct_of_portfolio":round(pos/portfolio_usd*100,1),
            "max_allowed_usd":round(max_position_usd,2),"tradeable":pos>=MIN_POSITION_USD}
    if verbose:
        print(f"\n{'='*50}\n  POSITION SIZER\n{'='*50}")
        print(f"  Signal score:     {signal_score}/100")
        print(f"  30d RV:           {rv:.1f}%  →  vol adj {vf:.2f}x")
        print(f"  Kelly (half):     {bk*KELLY_DAMPENER:.2%}  →  {rf:.2%} of portfolio")
        print(f"  Position size:    ${pos:,.2f} ({result['pct_of_portfolio']:.1f}%)")
        verdict="✅ TRADE" if result["tradeable"] else "❌ NO TRADE"
        print(f"  Verdict:          {verdict}\n{'='*50}\n")
    Path(".cache").mkdir(exist_ok=True)
    (Path(".cache")/"position_size.json").write_text(json.dumps(result,indent=2,default=str))
    return result

if __name__=="__main__":
    for s in [80,65,50,30]:
        compute_position_size(signal_score=s)

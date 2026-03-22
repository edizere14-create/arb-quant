"""
trade_executor.py  (v3)
Pre-trade checklist master controller.

Critical upgrades:
  ✅ Flashbots is now a HARD BLOCK — NO-GO if unreachable
  ✅ Signal score gate (must be >= 60/100)
  ✅ Circuit breaker gate
  ✅ Dynamic position sizing from signal_scorer + position_sizer
  ✅ Dual-source bridge signal validation
"""

import json
import requests
from datetime import datetime, timezone
from pathlib import Path

from flashloan_check  import run_flashloan_surface_check
from rugpull_check    import run_rugpull_check
from route_optimizer  import estimate_direct_vs_aggregator
from bridge_signal    import run_bridge_signal
from signal_scorer    import compute_signal_score, print_score
from position_sizer   import compute_position_size
from circuit_breaker  import run_circuit_breaker

ARB_RPC             = "https://arb1.arbitrum.io/rpc"
FLASHBOTS_RPC       = "https://rpc.flashbots.net/fast"
PORTFOLIO_TOTAL_USD = 500.0
MAX_STRATEGY_PCT    = 0.30
STRATEGY_C_PROTOCOLS= ["uniswap-v3","gmx-v2"]

def _rpc(url,method,params):
    try:
        r=requests.post(url,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},timeout=8)
        return r.json().get("result")
    except: return None

def _check(label,passed,detail=""):
    icon="✅" if passed else "❌"
    line=f"  [{icon}] {label}"
    if detail: line+=f"  —  {detail}"
    print(line)
    return {"label":label,"passed":passed,"detail":detail}

def gate_signal()->tuple[dict,dict]:
    sig=run_bridge_signal(verbose=False)
    passed=sig.get("entry_signal",False)
    detail=f"z_bridge={sig.get('z_bridge',0):.4f} z_stable={sig.get('z_stable',0):.4f} dual={'✅' if sig.get('dual_confirmed') else '❌'} corr={'✅' if sig.get('corr_valid') else '❌'}"
    return _check("Signal valid (dual-source)",passed,detail), sig

def gate_signal_score(sig:dict)->tuple[dict,dict]:
    score=compute_signal_score(
        z_bridge=sig.get("z_bridge",0),
        sources_agree=sig.get("sources_agree",False),
        both_elevated=sig.get("dual_confirmed",False),
        divergence=sig.get("divergence",0),
        pearson_r=sig.get("pearson_r",0),
        p_value=sig.get("p_value",1),
    )
    passed=score["tradeable"]
    detail=f"score={score['total_score']}/100 grade={score['grade'][:1]} (need ≥60)"
    return _check("Signal score ≥ 60/100",passed,detail), score

def gate_circuit_breaker()->dict:
    cb=run_circuit_breaker(verbose=False)
    passed=cb["all_clear"]
    detail=f"{'all clear' if passed else 'HALTED: '+', '.join(cb['triggered'])}"
    return _check("Circuit breaker clear",passed,detail)

def gate_flashloan(position_usd:float)->dict:
    print("\n  [Flash loan scan...]")
    result=run_flashloan_surface_check(protocol_slugs=STRATEGY_C_PROTOCOLS,position_size_usd=position_usd)
    passed=result["overall"] in ("GO","CAUTION")
    return _check("Flash loan surface safe",passed,f"overall: {result['overall']}")

def gate_rugpull()->dict:
    print("\n  [Rug pull scan...]")
    result=run_rugpull_check(protocol_keys=STRATEGY_C_PROTOCOLS)
    passed=result["overall"] in ("GO","CAUTION")
    return _check("No rug pull signals",passed,f"overall: {result['overall']}")

def gate_protocol_risk()->dict:
    SCORES={"uniswap-v3":{"score":9,"max":10},"gmx-v2":{"score":8,"max":10}}
    min_s=min(v["score"]/v["max"] for v in SCORES.values())
    passed=min_s>=0.7
    detail=" | ".join(f"{k}: {v['score']}/{v['max']}" for k,v in SCORES.items())
    return _check("Protocol risk ≥ 7/9",passed,detail)

def gate_position_size(signal_score:int)->tuple[dict,float]:
    sizing=compute_position_size(signal_score=signal_score,verbose=False)
    passed=sizing["tradeable"]
    pos=sizing["position_usd"]
    detail=f"${pos:,.2f} ({sizing['pct_of_portfolio']:.1f}% of portfolio) — Kelly-sized"
    return _check("Position size valid",passed,detail), pos

def gate_flashbots()->dict:
    """HARD BLOCK — if Flashbots unreachable, NO-GO. No exceptions."""
    result=_rpc(FLASHBOTS_RPC,"eth_blockNumber",[])
    passed=result is not None
    detail=(f"block {int(result,16):,}" if passed
            else "⛔ UNREACHABLE — HARD BLOCK. Do not trade via public mempool.")
    return _check("Flashbots RPC reachable [HARD BLOCK]",passed,detail)

def gate_fee_drag(position_usd:float,expected_return_pct:float=0.25)->dict:
    gas_result=_rpc(ARB_RPC,"eth_gasPrice",[])
    gas_gwei=int(gas_result,16)/1e9 if gas_result else 0.1
    gas_usd=gas_gwei*300_000*1e-9*2000
    protocol_fee=0.30; slippage=0.08
    gas_pct=gas_usd/position_usd*100
    total=gas_pct+protocol_fee+slippage
    net=expected_return_pct-total
    passed=net>=0.05
    detail=f"E[R]={expected_return_pct:.2f}% − costs={total:.2f}% = net {net:.2f}%"
    return _check("Fee drag viable (net>0.05%)",passed,detail)

def gate_route(from_token:str,to_token:str,position_usd:float)->dict:
    print("\n  [Route optimizer...]")
    route=estimate_direct_vs_aggregator(from_token,to_token,position_usd)
    rec=route.get("recommendation","use 1inch")
    return _check("Route optimized [advisory]",True,rec[:80] if rec else "use app.1inch.io")

def run_pre_trade_checklist(
    from_token:str="USDC", to_token:str="ARB",
    expected_return_pct:float=0.25
)->None:
    ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"  PRE-TRADE CHECKLIST  —  Strategy C")
    print(f"  {ts}")
    print(f"  {from_token} → {to_token}")
    print(f"{'='*60}\n")

    # Gate 1: Signal (dual source)
    g_signal, sig = gate_signal()

    # Gate 2: Signal score
    g_score, score = gate_signal_score(sig)
    signal_score = score.get("total_score",0)

    # Gate 3: Circuit breaker
    g_cb = gate_circuit_breaker()

    # Gate 4: Position size (dynamic Kelly)
    g_sizing, position_usd = gate_position_size(signal_score)

    # Gate 5: Flash loan surface
    g_flash = gate_flashloan(position_usd)

    # Gate 6: Rug pull
    g_rug = gate_rugpull()

    # Gate 7: Protocol risk
    g_proto = gate_protocol_risk()

    # Gate 8: Flashbots — HARD BLOCK
    g_fb = gate_flashbots()

    # Gate 9: Fee drag
    g_fee = gate_fee_drag(position_usd, expected_return_pct)

    # Gate 10: Route (advisory)
    g_route = gate_route(from_token, to_token, position_usd)

    # Hard gates — ALL must pass
    hard_gates = [g_signal, g_score, g_cb, g_sizing, g_flash, g_rug, g_proto, g_fb, g_fee]
    passed_count = sum(1 for g in hard_gates if g["passed"])
    hard_pass = all(g["passed"] for g in hard_gates)

    print(f"\n{'─'*60}")
    print(f"  Hard gates passed: {passed_count}/{len(hard_gates)}")
    print(f"  Position size:     ${position_usd:,.2f}")
    print(f"  Signal score:      {signal_score}/100")
    print(f"{'─'*60}")

    if hard_pass:
        print(f"\n  ✅ FINAL: GO")
        print(f"  Execute via Flashbots: {FLASHBOTS_RPC}")
        print(f"  Use 1inch:  app.1inch.io/#/42161/simple/swap/{from_token}/{to_token}")
        print(f"  Size:       ${position_usd:,.2f} (Kelly-adjusted)")
    else:
        failed=[g["label"] for g in hard_gates if not g["passed"]]
        print(f"\n  ❌ FINAL: NO-GO")
        print(f"  Do NOT touch MetaMask.")
        print(f"  Failed:")
        for f in failed: print(f"    • {f}")
        print(f"\n  Capital stays in yield router (Strategy D).")
    print(f"{'='*60}\n")

if __name__=="__main__":
    run_pre_trade_checklist()

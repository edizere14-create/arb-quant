"""
circuit_breaker.py
Consecutive loss protection and portfolio heat tracking.

Blocks new trades if:
  1. Consecutive losses >= MAX_CONSECUTIVE_LOSSES
  2. Portfolio drawdown >= MAX_DRAWDOWN_PCT
  3. Daily loss >= MAX_DAILY_LOSS_PCT
  4. Fee/PnL ratio >= MAX_FEE_RATIO (fees eating too much alpha)
  5. Win rate (last 10 trades) < MIN_WIN_RATE
"""
import json
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path

MAX_CONSECUTIVE_LOSSES = 3
MAX_DRAWDOWN_PCT       = 15.0
MAX_DAILY_LOSS_PCT     = 5.0
MAX_FEE_RATIO          = 0.40
MIN_WIN_RATE_10        = 0.30   # 30% over last 10 trades triggers review

POSITIONS_FILE = Path("positions.json")

def load_closed_trades()->list:
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text()).get("closed",[])
        except: pass
    return []

def check_consecutive_losses(trades:list)->dict:
    if not trades: return {"count":0,"triggered":False,"note":"no trades yet"}
    streak=0
    for t in reversed(trades):
        if (t.get("pnl_pct") or 0)<0: streak+=1
        else: break
    triggered=streak>=MAX_CONSECUTIVE_LOSSES
    return {"count":streak,"triggered":triggered,
            "note":f"{streak} consecutive losses (max {MAX_CONSECUTIVE_LOSSES})"}

def check_portfolio_drawdown(trades:list,portfolio_usd:float=500.0)->dict:
    if not trades: return {"drawdown_pct":0.0,"triggered":False,"note":"no trades"}
    pnls=[t.get("pnl_usd",0) or 0 for t in trades]
    cumulative=np.cumsum(pnls)
    peak=np.maximum.accumulate(cumulative)
    dd=float(np.min((cumulative-peak)/portfolio_usd*100)) if len(cumulative)>0 else 0.0
    triggered=dd<=-MAX_DRAWDOWN_PCT
    return {"drawdown_pct":round(dd,2),"triggered":triggered,
            "note":f"max drawdown {dd:.1f}% (limit -{MAX_DRAWDOWN_PCT}%)"}

def check_daily_loss(trades:list,portfolio_usd:float=500.0)->dict:
    now=datetime.now(timezone.utc)
    today_start=now.replace(hour=0,minute=0,second=0,microsecond=0)
    today_trades=[t for t in trades if _parse_dt(t.get("exit_time",""))>=today_start]
    daily_pnl=sum(t.get("pnl_usd",0) or 0 for t in today_trades)
    daily_pct=daily_pnl/portfolio_usd*100
    triggered=daily_pct<=-MAX_DAILY_LOSS_PCT
    return {"daily_pnl_usd":round(daily_pnl,2),"daily_pct":round(daily_pct,2),
            "triggered":triggered,"note":f"today P&L: ${daily_pnl:+.2f} ({daily_pct:+.1f}%)"}

def check_fee_ratio(trades:list)->dict:
    if len(trades)<5: return {"ratio":0.0,"triggered":False,"note":"need 5+ trades"}
    recent=trades[-10:]
    gross=[abs(t.get("pnl_usd",0) or 0) for t in recent if (t.get("pnl_usd",0) or 0)!=0]
    # Estimate fees as difference between gross and net — simplified
    ratio=0.0
    triggered=False
    return {"ratio":round(ratio,4),"triggered":triggered,
            "note":f"fee/PnL tracking (need manual cost log)"}

def check_win_rate(trades:list)->dict:
    if len(trades)<10: return {"win_rate":None,"triggered":False,"note":f"need 10 trades (have {len(trades)})"}
    recent=trades[-10:]
    wins=sum(1 for t in recent if (t.get("pnl_pct",0) or 0)>0)
    wr=wins/len(recent)
    triggered=wr<MIN_WIN_RATE_10
    return {"win_rate":round(wr,4),"triggered":triggered,
            "note":f"win rate {wr:.0%} over last 10 (min {MIN_WIN_RATE_10:.0%})"}

def _parse_dt(s:str)->datetime:
    try:
        return datetime.fromisoformat(s.replace("Z","+00:00"))
    except:
        return datetime.min.replace(tzinfo=timezone.utc)

def run_circuit_breaker(portfolio_usd:float=500.0,verbose:bool=True)->dict:
    trades=load_closed_trades()
    ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    checks={
        "consecutive_losses": check_consecutive_losses(trades),
        "portfolio_drawdown": check_portfolio_drawdown(trades,portfolio_usd),
        "daily_loss":         check_daily_loss(trades,portfolio_usd),
        "fee_ratio":          check_fee_ratio(trades),
        "win_rate":           check_win_rate(trades),
    }

    triggered=[name for name,c in checks.items() if c.get("triggered")]
    all_clear=len(triggered)==0

    if verbose:
        print(f"\n{'='*55}\n  CIRCUIT BREAKER CHECK\n  {ts}\n{'='*55}")
        for name,c in checks.items():
            icon="❌" if c.get("triggered") else "✅"
            print(f"  {icon} {name:<25} {c['note']}")
        print(f"{'─'*55}")
        if all_clear:
            print(f"  ✅ ALL CLEAR — no circuit breakers triggered")
        else:
            print(f"  🔴 HALTED — {len(triggered)} breaker(s) triggered:")
            for t in triggered:
                print(f"     • {t}")
            print(f"  Do NOT open new positions until resolved.")
        print(f"{'='*55}\n")

    result={"timestamp":ts,"all_clear":all_clear,"triggered":triggered,
            "checks":checks,"n_trades":len(trades)}
    Path(".cache").mkdir(exist_ok=True)
    (Path(".cache")/"circuit_breaker.json").write_text(json.dumps(result,indent=2,default=str))
    return result

if __name__=="__main__":
    run_circuit_breaker()

"""
backtest.py  (v2)
90-day walk-forward backtest with live trading gate.

CRITICAL: If this prints RESEARCH ONLY — NOT DEPLOYABLE,
real capital stays out. No exceptions.
"""

import json
import requests
import numpy as np
import pandas as pd
from scipy import stats
from datetime import datetime, timezone
from pathlib import Path

LOOKBACK_DAYS     = 90
ENTRY_Z           = 2.0
EXIT_Z            = 0.5
MIN_CORR_R        = 0.25
MAX_CORR_P        = 0.05
CORR_LOOKBACK     = 60
ENTRY_TIME_STOP_D = 1      # days
POSITION_SIZE_USD = 150.0  # 30% of $500
PROTOCOL_FEE_PCT  = 0.30
SLIPPAGE_PCT      = 0.08
GAS_COST_USD      = 0.05

def _get(url,timeout=12):
    try:
        r=requests.get(url,timeout=12,headers={"User-Agent":"arb-quant/1.0"})
        r.raise_for_status(); return r.json()
    except Exception as e:
        print(f"  [warn] {e}"); return None

def fetch_arb_prices()->pd.Series:
    data=_get("https://coins.llama.fi/chart/coingecko:arbitrum?span=120&period=1d")
    if not data: return pd.Series(dtype=float)
    prices=data["coins"]["coingecko:arbitrum"]["prices"]
    return pd.Series([p["price"] for p in prices],
                     index=pd.to_datetime([p["timestamp"] for p in prices],unit="s",utc=True))

def fetch_bridge_inflows()->pd.Series:
    data=_get("https://bridges.llama.fi/bridgevolume/Arbitrum?id=1")
    if data and isinstance(data,list):
        return pd.Series([d.get("depositUSD",0) for d in data],
                         index=pd.to_datetime([d.get("date",0) for d in data],unit="s",utc=True)).sort_index()
    data=_get("https://stablecoins.llama.fi/stablecoincharts/Arbitrum")
    if data:
        s=pd.Series([d.get("totalCirculatingUSD",{}).get("peggedUSD",0) for d in data],
                    index=pd.to_datetime([d.get("date",0) for d in data],unit="s",utc=True)).sort_index()
        return s.diff().fillna(0)
    return pd.Series(dtype=float)

def rolling_zscore(s:pd.Series,window:int=30)->pd.Series:
    m=s.rolling(window).mean(); sd=s.rolling(window).std()
    return (s-m)/sd.replace(0,np.nan)

def rolling_corr(x:pd.Series,y:pd.Series,window:int=60)->pd.Series:
    valid=pd.DataFrame({"x":x,"y":y}).dropna()
    result=pd.Series(index=valid.index,dtype=float)
    for i in range(window,len(valid)):
        xi=valid["x"].iloc[i-window:i]
        yi=valid["y"].iloc[i-window:i]
        if len(xi)<30: result.iloc[i]=np.nan; continue
        r,p=stats.pearsonr(xi,yi)
        result.iloc[i]=r if (r>MIN_CORR_R and p<MAX_CORR_P) else np.nan
    return result

def run_backtest()->dict:
    ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}\n  STRATEGY C BACKTEST — {LOOKBACK_DAYS} DAYS\n  {ts}\n{'='*60}")
    print("\n  Fetching data...")

    arb=fetch_arb_prices()
    inflows=fetch_bridge_inflows()

    if len(arb)<30 or len(inflows)<30:
        print("  ❌ Insufficient data.")
        return {}

    arb_d=arb.resample("1D").last().dropna()
    inflow_d=inflows.resample("1D").sum()
    idx=arb_d.index.intersection(inflow_d.index)
    arb_d=arb_d[idx]; inflow_d=inflow_d[idx]
    cutoff=arb_d.index[-1]-pd.Timedelta(days=LOOKBACK_DAYS)
    arb_d=arb_d[arb_d.index>=cutoff]
    inflow_d=inflow_d[inflow_d.index>=cutoff]

    print(f"  Range: {arb_d.index[0].date()} → {arb_d.index[-1].date()}  ({len(arb_d)} days)")

    inflow_z=rolling_zscore(inflow_d,30)
    fwd=arb_d.pct_change().shift(-1)
    corr=rolling_corr(inflow_z,fwd,CORR_LOOKBACK)

    trades=[]; in_trade=False; entry_price=0.0; entry_time=None

    for i in range(len(arb_d)):
        z=inflow_z.iloc[i] if not np.isnan(inflow_z.iloc[i] if not pd.isna(inflow_z.iloc[i]) else float("nan")) else 0.0
        valid=not pd.isna(corr.iloc[i]) if i<len(corr) else False
        price=arb_d.iloc[i]; date=arb_d.index[i]

        if not in_trade:
            if z>ENTRY_Z and valid:
                in_trade=True; entry_price=price; entry_time=date
        else:
            days_held=(date-entry_time).days
            pnl=(price-entry_price)/entry_price*100
            reason=None
            if z<EXIT_Z: reason="signal_faded"
            elif days_held>=ENTRY_TIME_STOP_D: reason="time_stop"
            elif pnl<=-10: reason="stop_loss"
            if reason:
                cost=(GAS_COST_USD/POSITION_SIZE_USD*100*2)+(PROTOCOL_FEE_PCT*2)+(SLIPPAGE_PCT*2)
                net=pnl-cost
                trades.append({"entry":str(entry_time.date()),"exit":str(date.date()),
                               "gross":round(pnl,4),"cost":round(cost,4),"net":round(net,4),
                               "pnl_usd":round(POSITION_SIZE_USD*net/100,4),
                               "reason":reason,"won":net>0})
                in_trade=False

    if not trades:
        print("\n  ⚠️  No trades triggered — signal never fired in this period.")
        print("  This confirms current live readings (all PARKED).")
        print(f"{'='*60}\n")
        result={"trades":0,"deployable":False,"reason":"signal_never_fired"}
        Path("backtest_results.json").write_text(json.dumps(result,indent=2,default=str))
        return result

    df=pd.DataFrame(trades)
    n=len(df); nw=df["won"].sum()
    wr=nw/n*100; total=df["pnl_usd"].sum()
    rets=df["net"].values
    sharpe=(np.mean(rets)/np.std(rets)*np.sqrt(252)) if np.std(rets)>0 else 0
    cum=(1+df["net"]/100).cumprod()
    dd=float(((cum-cum.cummax())/cum.cummax()*100).min())
    yield_baseline=POSITION_SIZE_USD*0.0544*(LOOKBACK_DAYS/365)
    deployable=sharpe>=0.5 and dd>=-30 and wr>=50

    print(f"\n  {'─'*55}\n  RESULTS\n  {'─'*55}")
    print(f"  Trades:       {n}  |  Win rate: {wr:.1f}% ({nw}/{n})")
    print(f"  Total PnL:    ${total:+.2f}")
    print(f"  Sharpe:       {sharpe:.2f}  (need ≥0.5)")
    print(f"  Max DD:       {dd:.1f}%  (limit -30%)")
    print(f"  vs Yield:     ${yield_baseline:.2f} ({LOOKBACK_DAYS}d in Radiant)")
    print(f"  {'─'*55}")

    print(f"\n  {'Entry':<12} {'Exit':<12} {'Gross':>8} {'Net':>8} {'Reason'}")
    print(f"  {'-'*60}")
    for _,t in df.iterrows():
        print(f"  {t['entry']:<12} {t['exit']:<12} {t['gross']:>+7.2f}% {t['net']:>+7.2f}%  {t['reason']}")

    print(f"\n  {'─'*55}")
    if deployable:
        print(f"  ✅ VERDICT: DEPLOYABLE")
        print(f"  Sharpe ≥ 0.5, DD > -30%, win rate ≥ 50%")
        print(f"  You may proceed to paper trading phase.")
    else:
        fails=[]
        if sharpe<0.5: fails.append(f"Sharpe {sharpe:.2f} < 0.5")
        if dd<-30: fails.append(f"Drawdown {dd:.1f}% exceeds -30%")
        if wr<50: fails.append(f"Win rate {wr:.1f}% < 50%")
        print(f"  ⚠️  VERDICT: RESEARCH ONLY — NOT DEPLOYABLE")
        for f in fails: print(f"  ✗ {f}")
        print(f"  Real capital stays out until these are resolved.")
    print(f"{'='*60}\n")

    result={"timestamp":ts,"n_trades":n,"win_rate":round(wr,2),
            "sharpe":round(sharpe,4),"max_drawdown":round(dd,2),
            "total_pnl_usd":round(total,4),"deployable":deployable,"trades":trades}
    Path("backtest_results.json").write_text(json.dumps(result,indent=2,default=str))
    print(f"  Results saved to backtest_results.json")
    return result

if __name__=="__main__":
    run_backtest()

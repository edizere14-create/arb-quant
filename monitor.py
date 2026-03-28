"""
monitor.py  (v3)
Daily signal monitor integrating all upgrades:
  - Dual-source bridge signal
  - Signal quality score
  - Circuit breaker check
  - Dynamic position sizing
  - Exit signal detection
  - Telegram alerts
  - Radiant TVL health check
"""

import os, json, requests
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, timezone
from pathlib import Path

from bridge_signal  import run_bridge_signal
from signal_scorer  import compute_signal_score, print_score
from position_sizer import compute_position_size
from circuit_breaker import run_circuit_breaker
from data_fetcher   import fetch_lending_lead_sync

LOG_FILE       = Path("signal_log.csv")
POSITIONS_FILE = Path("positions.json")
CACHE_DIR      = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)

ENTRY_Z      = 2.0
EXIT_Z       = 0.5
STOP_LOSS_PCT= -10.0
TIME_STOP_H  = 6

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID","")

def _get(url,timeout=10):
    try:
        r=requests.get(url,timeout=timeout,headers={"User-Agent":"arb-quant/1.0"})
        r.raise_for_status(); return r.json()
    except: return None

def send_telegram(msg:str)->None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"Markdown"},timeout=10)
    except Exception as e:
        print(f"  [telegram] {e}")

def fetch_fear_greed()->dict:
    """Crypto Fear & Greed Index (advisory only, not a hard gate)."""
    try:
        data=_get("https://api.alternative.me/fng/?limit=1")
        entry=data["data"][0]
        value=int(entry["value"])
        classification=entry["value_classification"]
        if value<25:     bias="CONTRARIAN_ENTRY"
        elif value>75:   bias="CONTRARIAN_EXIT"
        else:            bias="NEUTRAL"
        return {"value":value,"classification":classification,"bias":bias}
    except Exception:
        return {"value":-1,"classification":"UNAVAILABLE","bias":"NEUTRAL"}

def fetch_arb_price()->float:
    try:
        data=_get("https://coins.llama.fi/prices/current/coingecko:arbitrum")
        return float(data["coins"]["coingecko:arbitrum"]["price"])
    except: return 0.0

def fetch_eth_price()->float:
    try:
        data=_get("https://coins.llama.fi/prices/current/coingecko:ethereum")
        return float(data["coins"]["coingecko:ethereum"]["price"])
    except: return 2000.0

def load_positions()->dict:
    if POSITIONS_FILE.exists():
        try: return json.loads(POSITIONS_FILE.read_text())
        except: pass
    return {"open":[],"closed":[]}

def save_positions(p:dict)->None:
    POSITIONS_FILE.write_text(json.dumps(p,indent=2,default=str))

def check_exits(positions:dict,z:float,price:float)->list:
    to_close=[]
    now=datetime.now(timezone.utc)
    for pos in positions["open"]:
        reasons=[]
        if z<EXIT_Z: reasons.append(f"z={z:.4f} faded")
        try:
            entry_dt=datetime.fromisoformat(pos["entry_time"])
            if (now-entry_dt).total_seconds()/3600>=TIME_STOP_H:
                reasons.append(f"time stop")
        except: pass
        if price>0 and pos.get("entry_price",0)>0:
            pnl=(price-pos["entry_price"])/pos["entry_price"]*100
            if pnl<=STOP_LOSS_PCT: reasons.append(f"stop loss {pnl:.1f}%")
        if reasons: to_close.append({"position":pos,"reasons":reasons})
    return to_close

def close_position(pos_id:str,exit_price:float,reason:str)->dict|None:
    positions=load_positions()
    for i,pos in enumerate(positions["open"]):
        if pos["id"]==pos_id:
            pnl_pct=(exit_price-pos["entry_price"])/pos["entry_price"]*100
            pos.update({"exit_time":datetime.now(timezone.utc).isoformat(),
                       "exit_price":exit_price,"exit_reason":reason,"status":"closed",
                       "pnl_pct":round(pnl_pct,4),"pnl_usd":round(pos["size_usd"]*pnl_pct/100,4)})
            positions["closed"].append(pos)
            positions["open"].pop(i)
            save_positions(positions)
            return pos
    return None

_LOG_COLUMNS = ["timestamp","z_score","pearson_r","p_value","corr_valid",
                "entry_signal","decision","fng_value","fng_bias"]


def _migrate_log_if_needed() -> None:
    """If signal_log.csv has the old 7-column header, migrate to 9 columns."""
    if not LOG_FILE.exists():
        return
    lines = LOG_FILE.read_text().strip().splitlines()
    if not lines:
        return
    header = lines[0].split(",")
    if header == _LOG_COLUMNS:
        return  # already correct

    new_lines = [",".join(_LOG_COLUMNS)]
    for line in lines[1:]:
        vals = line.split(",")
        # Old 7-column rows: ts, z_score, pearson_r, p_value, corr_valid, entry_signal, decision
        if len(vals) == 7:
            new_lines.append(line + ",NA,NA")
        # Malformed 11-value rows from broken writer:
        # ts, z_bridge(0.0), z_stable(-0.4101), p(0.0), cv(1.0), F(alse), score(45),
        # entry(False), decision(PARKED), fng_value(12), fng_bias(CONTRARIAN_ENTRY)
        elif len(vals) == 11:
            ts, _, _, _, _, _, _, entry, decision, fng_v, fng_b = vals
            new_lines.append(f"{ts},NA,NA,NA,NA,{entry},{decision},{fng_v},{fng_b}")
        else:
            # Unknown format — skip
            continue
    LOG_FILE.write_text("\n".join(new_lines) + "\n")


def log_signal(ts,z_b,r,p,cv,entry,decision,fng_value=-1,fng_bias="NEUTRAL")->None:
    _migrate_log_if_needed()
    row=pd.DataFrame([{"timestamp":ts,"z_score":round(z_b,4),
                        "pearson_r":round(r,4),"p_value":round(p,4),"corr_valid":cv,
                        "entry_signal":entry,"decision":decision,
                        "fng_value":fng_value,"fng_bias":fng_bias}])
    header=not LOG_FILE.exists()
    row.to_csv(LOG_FILE,mode="a",header=header,index=False)

def run_monitor():
    now=datetime.now(timezone.utc)
    ts=now.strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n[monitor] {ts}")

    # ── Prices ────────────────────────────────────────────────────────────────
    eth_price=fetch_eth_price()
    arb_price=fetch_arb_price()

    # ── Fear & Greed (advisory) ────────────────────────────────────────────────
    fng=fetch_fear_greed()
    fng_v=fng["value"]; fng_cls=fng["classification"]; fng_bias=fng["bias"]
    bias_arrow="↑" if fng_bias=="CONTRARIAN_ENTRY" else "↓" if fng_bias=="CONTRARIAN_EXIT" else "→"
    print(f"  [fear_greed] value={fng_v}, {fng_cls} {bias_arrow} {fng_bias} bias")

    # ── Bridge signal (dual source) ───────────────────────────────────────────
    sig=run_bridge_signal(verbose=False)
    z_b=sig.get("z_bridge",0); z_s=sig.get("z_stable",0)
    r=sig.get("pearson_r",0); p=sig.get("p_value",1)
    cv=sig.get("corr_valid",False)

    # ── Signal score ──────────────────────────────────────────────────────────
    score_result=compute_signal_score(
        z_bridge=z_b,sources_agree=sig.get("sources_agree",False),
        both_elevated=sig.get("dual_confirmed",False),
        divergence=sig.get("divergence",0),pearson_r=r,p_value=p)
    signal_score=score_result["total_score"]

    # ── Circuit breaker ───────────────────────────────────────────────────────
    cb=run_circuit_breaker(verbose=False)

    # ── Check exits ───────────────────────────────────────────────────────────
    positions=load_positions()
    exits=check_exits(positions,z_b,arb_price)
    for ex in exits:
        pos=ex["position"]; reasons=ex["reasons"]
        closed=close_position(pos["id"],arb_price," | ".join(reasons))
        if closed:
            msg=(f"🔴 *EXIT — Strategy {pos['strategy']}*\n"
                 f"Asset: {pos['asset']} | PnL: {closed['pnl_pct']:+.2f}%\n"
                 f"Reason: {' | '.join(reasons)}")
            print(f"  🔴 EXIT {pos['asset']} {closed['pnl_pct']:+.2f}% — {' | '.join(reasons)}")
            send_telegram(msg)

    # ── Lending lead (Aave V3 first, Radiant fallback) ─────────────────────
    lending = fetch_lending_lead_sync()
    yield_rec = lending["protocol"]
    print(f"  🏦 Lending lead: {yield_rec}  TVL ${lending['tvl']:,.0f}")

    # ── Entry decision ────────────────────────────────────────────────────────
    entry_signal=(sig.get("entry_signal",False) and
                  score_result["tradeable"] and
                  cb["all_clear"])

    if entry_signal:
        sizing=compute_position_size(signal_score=signal_score,verbose=False)
        decision="ENTRY SIGNAL"
        fng_line=f"F&G: {fng_v} ({fng_cls}) → {fng_bias.lower().replace('_',' ')} bias"
        msg=(f"⚡ *ENTRY SIGNAL — Strategy C*\n"
             f"Score: {signal_score}/100 ({score_result['grade'][:1]})\n"
             f"z_bridge={z_b:.4f} z_stable={z_s:.4f}\n"
             f"r={r:.4f} p={p:.4f}\n"
             f"Position size: ${sizing['position_usd']:.2f}\n"
             f"ARB: ${arb_price:.4f}\n"
             f"{fng_line}\n"
             f"→ Run `trade_executor.py` before MetaMask")
        print(f"  ⚡ ENTRY SIGNAL  score={signal_score}  z={z_b:.4f}  r={r:.4f}")
        send_telegram(msg)
    else:
        decision="PARKED"
        reasons=[]
        if not sig.get("entry_signal"):
            if not sig.get("dual_confirmed"): reasons.append(f"z={z_b:.4f}<{ENTRY_Z} or sources diverge")
            if not cv: reasons.append(f"corr decayed r={r:.4f}")
        if not score_result["tradeable"]: reasons.append(f"score={signal_score}<60")
        if not cb["all_clear"]: reasons.append(f"circuit breaker: {','.join(cb['triggered'])}")
        print(f"  ⏸ PARKED  [{' | '.join(reasons) if reasons else 'no signal'}]")
        if TELEGRAM_BOT_TOKEN:
            fng_line=f"F&G: {fng_v} ({fng_cls}) → {fng_bias.lower().replace('_',' ')} bias"
            send_telegram(f"📊 *Daily Monitor {now.strftime('%Y-%m-%d')}*\n"
                         f"PARKED | score={signal_score} z={z_b:.4f} r={r:.4f}\n"
                         f"ARB ${arb_price:.4f} | Yield→{yield_rec}\n"
                         f"{fng_line}")

    log_signal(ts,z_b,r,p,cv,entry_signal,decision,fng_v,fng_bias)
    print(f"  📝 Logged to {LOG_FILE}")

if __name__=="__main__":
    run_monitor()

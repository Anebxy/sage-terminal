import sys,os,json,datetime as dt;sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from engine import stats,db,DATA as D
s=stats();t=dt.date.today().isoformat();c=db()
tr=c.execute("select symbol,cost,proceeds,exit_reason from positions where closed>=?",(t,)).fetchall()
L=[f"PAPER TERMINAL daily summary {s['ts']} (SIMULATED)",f"Equity ${s['equity']}  total PnL ${s['pnl']}  cash ${s['cash']}",
 f"Funnel: {json.dumps(s['funnel'])}",f"Closed {s['closed']} win rate {s['win_rate']} realized PnL after fees ${s['realized_pnl_after_fees']} fees ${s['fees']}",
 "Top kill reasons: "+"; ".join(f"{k['k']}x{k['n']}" for k in s['top_kill_reasons']),"Open: "+", ".join(f"{p['symbol']} {p['pnl_pct']}%" for p in s['open']),
 "Closed today:"]+[f"  {r[0]} cost {r[1]:.2f} -> {r[2]:.2f} ({r[3]})" for r in tr]
txt="\n".join(L);open(f"{D}/daily_summary.txt","w").write(txt+"\n");print(txt)

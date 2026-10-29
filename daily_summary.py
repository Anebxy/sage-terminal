import os,json,sqlite3,datetime as dt
D=os.path.dirname(os.path.abspath(__file__));DATA=os.environ.get("DATA_DIR") or D
c=sqlite3.connect(f"{DATA}/sage.db");c.row_factory=sqlite3.Row;cfg=json.load(open(f"{D}/config.json"));t=dt.date.today().isoformat()
cash=cfg["start_equity"]+sum(r["proceeds"]-r["cost"] for r in c.execute("select * from positions"))
op=c.execute("select * from positions where status='open'").fetchall();eq=cash+sum(p["remaining"]*p["last"] for p in op)
cl=c.execute("select * from positions where closed>=?",(t,)).fetchall();w=sum(1 for p in cl if p["proceeds"]>p["cost"])
k=c.execute("select reason,count(*) n from signals where ts>=? group by reason order by n desc limit 6",(t,)).fetchall()
L=[f"SAGE BOT daily summary {dt.datetime.now():%Y-%m-%d %H:%M} (PAPER / SIMULATED)",f"Equity ${eq:,.2f} (start ${cfg['start_equity']:,.2f}), open {len(op)}",
 f"Closed today {len(cl)}, wins {w}",f"Top skip reasons: "+"; ".join(f"{r['reason']} x{r['n']}" for r in k)]+[f"  ${p['sym']} {p['cost']:.2f} -> {p['proceeds']:.2f} ({p['exit_reason']})" for p in cl]
txt="\n".join(L);open(f"{DATA}/daily_summary.txt","w").write(txt+"\n");print(txt)

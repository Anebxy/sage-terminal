"""Major-crypto paper desk. Same signal/fill code as the backtest (cbt.py) so live == tested logic.
Paper only: public Binance market data (data-api.binance.vision), no keys, no orders sent anywhere."""
import asyncio,json,time,os,datetime as dt
import cbt
FEE,SLIP=cbt.FEE,cbt.SLIP
CHARTERS={
 "EXECUTIVE":"Splits paper capital: ~90% crypto desk, ~10% memecoin sniper. Only funds configs that were positive on held-out data after fees.",
 "SCREENER":"Pulls 1H candles (4H built from them) for the 10-coin universe from Binance public data every minute; flags coins whose 4H trend is up.",
 "ENTRY PLANNER":"On each closed 1H bar, checks each funded config's setup (trend breakout or oversold bounce). Plans entry, stop (ATR-based) and target before any buy.",
 "RISK OFFICER":"Sizes every trade so hitting the stop loses 1.5% of the config's capital; caps a position at 35% and 4 open per config. Vetoes trades with no valid stop.",
 "EXIT CLERK":"Watches live prices every 10s. Exits at stop (filled at the worse of stop or current price, minus slippage), target, trailing stop, or time limit.",
 "AUDITOR":"Logs every fill with fees (0.1%/side) and slippage (0.05%/side). Keeps the go/no-go scorecard. Replaces a config only after >=30 trades of evidence."}
THRESH=dict(trades=40,wr=0.35,net=0.0,mdd=0.15)
class Desk:
    def __init__(s,db,say,live,D):
        s.db=db;s.say=say;s.live=live;s.dir=D;s.bars={};s.I={};s.px={};s.last_bar={};s.ok=0;s.peak=0;s.mdd=0
        db.executescript("""create table if not exists c_cfg(id integer primary key,name text,cfg text,status text,cash real,start real,born text,died text,note text,lessons text);
        create table if not exists c_pos(id integer primary key,cfg int,coin text,opened text,opened_ts real,entry real,qty real,cost real,stop real,tp real,bar int,status text,closed text,exit_px real,proceeds real,fees real,slip real,exit_reason text,plan text);""");db.commit()
        s.R=json.load(open(f"{D}/crypto_bt.json"))
    def cfgs(s):return [dict(r) for r in s.db.execute("select * from c_cfg where status='alive'")]
    def seed(s,capital):
        fund=[x for x in s.R["ranked"] if x["funded"]][:s.R.get("max_funded",3)]
        for i,x in enumerate(fund):
            s.db.execute("insert into c_cfg(name,cfg,status,cash,start,born,note,lessons) values(?,?,?,?,?,?,?,?)",(x["name"],json.dumps(x["cfg"]),"alive",capital/len(fund),capital/len(fund),dt.datetime.now().isoformat(),x["why"],""))
        s.db.commit();s.say("EXECUTIVE","EVOLVE",f"Funded {len(fund)} held-out-positive configs with ${capital:.2f}: "+", ".join(x['name'] for x in fund))
    async def candles(s,http,coin):
        u=f"https://data-api.binance.vision/api/v3/klines?symbol={coin}USDT&interval=1h&limit=600"
        async with http.get(u,timeout=15) as r:d=await r.json()
        return [[k[0]//1000,float(k[1]),float(k[2]),float(k[3]),float(k[4]),float(k[7])] for k in d[:-1]]  # drop the still-open bar
    async def prices(s,http):
        u="https://data-api.binance.vision/api/v3/ticker/price?symbols="+json.dumps([c+"USDT" for c in cbt.U]).replace(" ","")
        async with http.get(u,timeout=10) as r:d=await r.json()
        for x in d:s.px[x["symbol"][:-4]]=float(x["price"])
        s.ok=time.time()
    def equity(s,c=None):
        out=0
        for k in ([c] if c else s.cfgs()):
            out+=k["cash"]+sum(p["qty"]*s.px.get(p["coin"],p["entry"]) for p in s.db.execute("select * from c_pos where cfg=? and status='open'",(k["id"],)))
        return out
    async def bar_loop(s,http):
        while True:
            try:
                for c in cbt.U:
                    b=await s.candles(http,c);s.bars[c]=b;s.I[c]=cbt.ind(b)
                    await asyncio.sleep(0.3)
                up=[c for c in cbt.U if s.I[c]["up"][-1]]
                s.live("SCREENER",f"4H uptrend: {', '.join(up) or 'none'} ({len(up)}/10)")
                t=s.bars["BTC"][-1][0]
                if s.last_bar.get("t")!=t:s.last_bar["t"]=t;s.on_bar()
            except Exception as e:s.live("SCREENER",f"data error: {type(e).__name__} (retrying)")
            await asyncio.sleep(60)
    def on_bar(s):
        planned=0
        for k in s.cfgs():
            g=json.loads(k["cfg"]);eq=s.equity(k)
            opn=[p["coin"] for p in s.db.execute("select coin from c_pos where cfg=? and status='open'",(k["id"],))]
            for c in cbt.U:
                I=s.I.get(c)
                if not I or c in opn or len(opn)>=4:continue
                i=len(I["c"])-1
                last=s.db.execute("select max(opened_ts) from c_pos where cfg=? and coin=?",(k["id"],c)).fetchone()[0] or 0
                if time.time()-last<g.get("cool",6)*3600:continue
                if not cbt.signal(g,I,i):continue
                px=s.px.get(c,I["c"][i]);e=px*(1+SLIP);st=e-g["k"]*I["atr"][i]
                if st<=0 or (e-st)/e<0.004:s.say("RISK OFFICER","VETO",f"{k['name']} {c}: stop too tight/invalid");continue
                q=min(0.015*eq/(e-st),0.35*eq/e);cost=q*e*(1+FEE)
                if cost>k["cash"]:s.say("RISK OFFICER","VETO",f"{k['name']} {c}: not enough free cash (${k['cash']:.2f})");continue
                tp=e+g["tp"]*(e-st) if g.get("tp") else None
                plan=f"{g['s']} setup · entry ${e:,.4g} · stop ${st:,.4g} ({(st/e-1)*100:.1f}%)"+(f" · target ${tp:,.4g}" if tp else f" · trail {g['trail']}×ATR")+f" · risk ${0.015*eq:.2f}"
                s.say("ENTRY PLANNER","PLAN",f"{k['name']} {c}: {plan}")
                s.db.execute("insert into c_pos(cfg,coin,opened,opened_ts,entry,qty,cost,stop,tp,bar,status,fees,slip,plan) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(k["id"],c,dt.datetime.now().isoformat(),time.time(),e,q,cost,st,tp,0,"open",q*e*FEE,q*px*SLIP,plan))
                s.db.execute("update c_cfg set cash=cash-? where id=?",(cost,k["id"]));k["cash"]-=cost;opn.append(c);planned+=1
                s.say("AUDITOR","BUY",f"{k['name']} BUY {c} {q:.6g} @ ${e:,.4g} · cost ${cost:.2f} (fee ${q*e*FEE:.2f}, slip ${q*px*SLIP:.2f})")
            # bar-based exits: trailing stop update + time stop
            for p in s.db.execute("select * from c_pos where cfg=? and status='open'",(k["id"],)).fetchall():
                I=s.I.get(p["coin"]);n=p["bar"]+1;s.db.execute("update c_pos set bar=? where id=?",(n,p["id"]))
                if n>=g["hold"]:s.close(p,s.px.get(p["coin"],I["c"][-1]),"time limit")
                elif g["s"]=="trend":
                    ns=max(p["stop"],I["c"][-1]-g["trail"]*I["atr"][-1])
                    if ns>p["stop"]:s.db.execute("update c_pos set stop=? where id=?",(ns,p["id"]));s.say("EXIT CLERK","TRAIL",f"{p['coin']} stop raised to ${ns:,.4g}")
        s.db.commit();s.live("ENTRY PLANNER",f"bar {dt.datetime.fromtimestamp(s.last_bar['t']+3600).strftime('%H:%M')} checked · {planned} new plan(s)")
    def close(s,p,px,why):
        fill=px*(1-SLIP);proc=p["qty"]*fill*(1-FEE);fees=p["fees"]+p["qty"]*fill*FEE;slip=p["slip"]+p["qty"]*px*SLIP
        s.db.execute("update c_pos set status='closed',closed=?,exit_px=?,proceeds=?,fees=?,slip=?,exit_reason=? where id=?",(dt.datetime.now().isoformat(),fill,proc,fees,slip,why,p["id"]))
        s.db.execute("update c_cfg set cash=cash+? where id=?",(proc,p["cfg"]));s.db.commit()
        pnl=proc-p["cost"];k=s.db.execute("select * from c_cfg where id=?",(p["cfg"],)).fetchone()
        s.say("AUDITOR","SELL",f"{k['name']} SELL {p['coin']} @ ${fill:,.4g} · {why} · net {'+' if pnl>=0 else '−'}${abs(pnl):.2f} after ${fees:.2f} fees + ${slip:.2f} slippage",pnl=pnl)
        s.evolve(k)
    async def exit_loop(s,http):
        while True:
            try:
                await s.prices(http)
                for p in s.db.execute("select * from c_pos where status='open'").fetchall():
                    px=s.px.get(p["coin"])
                    if not px:continue
                    if px<=p["stop"]:s.close(p,px,"stop")
                    elif p["tp"] and px>=p["tp"]:s.close(p,p["tp"],"target")
                n=s.db.execute("select count(*) from c_pos where status='open'").fetchone()[0]
                s.live("EXIT CLERK",f"watching {n} open position(s) · prices {dt.datetime.now().strftime('%H:%M:%S')}")
            except Exception as e:s.live("EXIT CLERK",f"price error: {type(e).__name__}")
            await asyncio.sleep(10)
    def evolve(s,k):
        """Replace a config only on evidence: >=30 closed trades and negative net after fees, or >15% drawdown of its start (hard safety)."""
        cl=s.db.execute("select proceeds-cost from c_pos where cfg=? and status='closed'",(k["id"],)).fetchall();net=sum(r[0] for r in cl)
        eq=s.equity(dict(k));dd=1-eq/k["start"]
        why=f"{len(cl)} trades, net ${net:+.2f}" if len(cl)>=30 and net<0 else (f"drawdown {dd:.0%} > 15%" if dd>0.15 else None)
        if not why:return
        used={x["name"] for x in (dict(r) for r in s.db.execute("select name from c_cfg"))}
        nxt=[x for x in s.R["ranked"] if x["funded"] and x["name"] not in used]
        for p in s.db.execute("select * from c_pos where cfg=? and status='open'",(k["id"],)).fetchall():s.close(p,s.px.get(p["coin"],p["entry"]),"config replaced")
        k=dict(s.db.execute("select * from c_cfg where id=?",(k["id"],)).fetchone())
        s.db.execute("update c_cfg set status='replaced',died=?,note=note||? where id=?",(dt.datetime.now().isoformat()," · replaced: "+why,k["id"]))
        if nxt:
            x=nxt[0];old=json.loads(k["cfg"]);lesson=f"{k['name']} ({old['s']}) was replaced after {why}. You will be replaced on the same evidence rule."
            s.db.execute("insert into c_cfg(name,cfg,status,cash,start,born,note,lessons) values(?,?,?,?,?,?,?,?)",(x["name"],json.dumps(x["cfg"]),"alive",k["cash"],k["cash"],dt.datetime.now().isoformat(),x["why"],lesson))
            s.say("AUDITOR","EVOLVE",f"Replaced {k['name']} ({why}) with {x['name']}")
        else:
            best=s.db.execute("select id from c_cfg where status='alive' limit 1").fetchone()
            if best:s.db.execute("update c_cfg set cash=cash+? where id=?",(k["cash"],best[0]))
            s.say("AUDITOR","EVOLVE",f"Retired {k['name']} ({why}); no untested held-out-positive config left, cash moved to survivors")
        s.db.commit()
    def scorecard(s,t0,sol0):
        cl=[dict(r) for r in s.db.execute("select * from c_pos where status='closed' order by id")]
        n=len(cl);w=sum(1 for r in cl if r["proceeds"]>r["cost"]);net=sum(r["proceeds"]-r["cost"] for r in cl)
        eqs=[r[0] for r in s.db.execute("select crypto from equity2 order by ts")]
        pk=0;mdd=0
        for e in eqs:pk=max(pk,e);mdd=max(mdd,1-e/pk if pk else 0)
        ret=(eqs[-1]/eqs[0]-1) if eqs else 0;solr=(s.px.get("SOL",0)/sol0-1) if sol0 else 0
        rows=[dict(k="Trades",v=n,need=f">= {THRESH['trades']}",ok=n>=THRESH["trades"]),dict(k="Win rate",v=f"{w/max(n,1):.0%}",need=">= 35%",ok=n>0 and w/n>=THRESH["wr"]),
              dict(k="Net after fees",v=f"${net:+.2f}",need="> $0",ok=net>0),dict(k="Max drawdown",v=f"{mdd:.1%}",need="<= 15%",ok=mdd<=THRESH["mdd"]),
              dict(k="vs hold SOL",v=f"{ret:+.1%} vs {solr:+.1%}",need="beat it",ok=ret>solr)]
        return dict(rows=rows,go=all(r["ok"] for r in rows),fees=round(sum(r["fees"] or 0 for r in cl),2),slip=round(sum(r["slip"] or 0 for r in cl),2))
    def view(s):
        cf=[]
        for k in [dict(r) for r in s.db.execute("select * from c_cfg order by status='alive' desc,id desc limit 12")]:
            cl=s.db.execute("select proceeds-cost from c_pos where cfg=? and status='closed'",(k["id"],)).fetchall()
            g=json.loads(k["cfg"]);eq=s.equity(k) if k["status"]=="alive" else k["cash"]
            cf.append(dict(name=k["name"],status=k["status"],style=g["s"],g=g,equity=round(eq,2),start=round(k["start"],2),pnl=round(eq-k["start"],2),trades=len(cl),wins=sum(1 for r in cl if r[0]>0),note=k["note"],lessons=k["lessons"]))
        pos=[];
        for p in s.db.execute("select p.*,c.name cn from c_pos p join c_cfg c on c.id=p.cfg where p.status='open' order by p.id desc"):
            px=s.px.get(p["coin"],p["entry"]);v=p["qty"]*px*(1-FEE)
            pos.append(dict(coin=p["coin"],cfg=p["cn"],entry=p["entry"],px=px,stop=p["stop"],tp=p["tp"],cost=round(p["cost"],2),pnl=round(v-p["cost"],2),r=round(100*(v/p["cost"]-1),2),opened=p["opened"],plan=p["plan"]))
        done=[dict(coin=p["coin"],cfg=p["cn"],entry=p["entry"],exit=p["exit_px"],pnl=round(p["proceeds"]-p["cost"],2),r=round(100*(p["proceeds"]/p["cost"]-1),2),fees=round(p["fees"],2),slip=round(p["slip"],2),why=p["exit_reason"],closed=p["closed"],plan=p["plan"]) for p in s.db.execute("select p.*,c.name cn from c_pos p join c_cfg c on c.id=p.cfg where p.status='closed' order by p.id desc limit 30")]
        up=[c for c in cbt.U if c in s.I and s.I[c]["up"][-1]]
        return dict(configs=cf,open=pos,done=done,px={c:s.px.get(c) for c in cbt.U},up=up,feed=time.time()-s.ok<60,bt=dict(train=s.R["train"],hold=s.R["hold"],solbh=s.R["solbh"],ranked=s.R["ranked"][:12]),charters=CHARTERS)

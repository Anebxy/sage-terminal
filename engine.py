"""SAGE TERMINAL - PAPER TRADING ONLY. No wallets, keys, signing, or transactions. Reads free public APIs only."""
import json,time,sqlite3,threading,traceback,requests,os,datetime as dt
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
D=os.path.dirname(os.path.abspath(__file__))
DATA=os.environ.get("DATA_DIR") or D
try:os.makedirs(DATA,exist_ok=True);open(f"{DATA}/.w","w").close()
except Exception:DATA=D
DB=f"{DATA}/paper.db"
C=lambda:json.load(open(f"{D}/config.json"))
S=requests.Session();S.headers["User-Agent"]="Mozilla/5.0 (sage-terminal paper)";_last={}
def get(url,host_gap=1.2,quiet=False):
    h=url.split("/")[2];w=_last.get(h,0)+host_gap-time.time()
    if w>0:time.sleep(w)
    _last[h]=time.time();err=None
    for i in range(3):
        try:
            r=S.get(url,timeout=15)
            if r.status_code==429:time.sleep(5*(i+1));continue
            r.raise_for_status();return r.json()
        except Exception as e:err=e;time.sleep(2)
    if not quiet:log("api_error",{"url":url[:120],"err":str(err)[:200]})
    return None
def db():
    c=sqlite3.connect(DB,timeout=30);c.row_factory=sqlite3.Row;return c
def now():return dt.datetime.now().isoformat(timespec="seconds")
AGENTS=["SCOUT","SOCIAL","RUG GUARD","TRACKER","TRADER","RISK"]
SEATS={k:dict(status="idle",line="waiting for first cycle",ts="",n=0) for k in AGENTS}
TASKS=[0]
def seat(k,status,line):
    x=SEATS[k];x.update(status=status,line=str(line)[:150],ts=now());x["n"]+=1;TASKS[0]+=1
_conn=None
def log(kind,data):
    try:c=_conn or db();c.execute("insert into events(ts,kind,data) values(?,?,?)",(now(),kind,json.dumps(data)));c.commit()
    except Exception:pass
def L(tag,agent,msg,**kw):log(tag,dict(msg=msg,agent=agent,**kw))
def init():
    c=db();c.executescript("""create table if not exists events(id integer primary key,ts text,kind text,data text);
    create table if not exists signals(id integer primary key,ts text,token text,symbol text,pair text,source text,liq real,score real,status text,reason text);
    create table if not exists positions(id integer primary key,token text,symbol text,pair text,opened text,closed text,entry real,qty real,cost real,peak real,last real,remaining real,proceeds real default 0,fees real default 0,status text,exit_reason text,tp_hit int default 0);
    create table if not exists equity(ts text,equity real,cash real);""")
    for t,col in [("signals","judge text"),("signals","analyst real"),("signals","agent text"),("positions","entry_reason text"),("positions","stop real"),("positions","creator text"),("positions","dev_bal real")]:
        try:c.execute(f"alter table {t} add column {col}")
        except Exception:pass
    c.commit()
# ---------- SCOUT: pump.fun public frontend API (read-only, no key) + DexScreener/GeckoTerminal fallback ----------
PF="https://frontend-api-v3.pump.fun/coins?offset=0&limit=50&order=DESC&includeNsfw=false&sort="
def candidates(cfg):
    out={};meta={};npf=0
    for sort in ("created_timestamp","last_trade_timestamp"):
        for t in get(PF+sort,1.5) or []:
            if not isinstance(t,dict) or not t.get("mint"):continue
            npf+=1;age=(time.time()*1000-(t.get("created_timestamp") or 0))/6e4
            if (t.get("market_cap_usd") or 0)>=cfg["pf_min_mcap"] and age<=cfg["pf_max_age_min"]:
                out.setdefault(t["mint"],"pump.fun");meta[t["mint"]]=t
    for src,url in [("dex_profiles","https://api.dexscreener.com/token-profiles/latest/v1"),("dex_boost_paid","https://api.dexscreener.com/token-boosts/latest/v1")]:
        for t in get(url) or []:
            if t.get("chainId")=="solana":out.setdefault(t["tokenAddress"],src)
    g=get("https://api.geckoterminal.com/api/v2/networks/solana/new_pools",2.5)
    for p in (g or {}).get("data",[]):
        a=p["relationships"]["base_token"]["data"]["id"].split("_",1)[1];out.setdefault(a,"gecko_new")
    return out,meta,npf
def pairs_for(tokens):
    res={}
    for i in range(0,len(tokens),30):
        for p in get("https://api.dexscreener.com/tokens/v1/solana/"+",".join(tokens[i:i+30])) or []:
            a=p["baseToken"]["address"]
            if a in tokens and (a not in res or (p.get("liquidity") or {}).get("usd",0)>(res[a].get("liquidity") or {}).get("usd",0)):res[a]=p
    return res
# ---------- pump.fun bonding-curve liquidity (pre-graduation) ----------
def curve_liq(p,m):
    """Virtual SOL reserves from curve market cap (constant product, 30 SOL / 1.073B virtual start), x2 sides, in USD."""
    try:
        mc_usd=m.get("market_cap_usd") or p.get("marketCap") or p.get("fdv") or 0
        sol_usd=(m["market_cap_usd"]/m["market_cap"]) if m.get("market_cap") else float(p["priceUsd"])/float(p["priceNative"])
        vsol=(mc_usd/sol_usd*30*1.073e9/1e9)**0.5
        return round(2*vsol*sol_usd),round(vsol,1)
    except Exception:return 0,0
# ---------- SOCIAL: link presence + website reachability. Does NOT read X (no API access). ----------
def social(p,m):
    info=(p or {}).get("info") or {};links={}
    for s in info.get("socials") or []:links[s.get("type")]=s.get("url")
    for w in info.get("websites") or []:links.setdefault("website",w.get("url"))
    for k in ("twitter","telegram","website"):
        if m.get(k):links.setdefault(k,m[k])
    tw=links.get("twitter") or "";notes=[]
    if not links:return None,"no X/website/telegram links",links
    if "/status/" in tw:notes.append("X link is a tweet, not a project account")
    elif tw:notes.append("X account linked")
    web=links.get("website")
    if web and "x.com" not in web and "twitter.com" not in web:
        try:r=S.head(web,timeout=6,allow_redirects=True);notes.append(f"site {'up' if r.status_code<400 else 'down '+str(r.status_code)}")
        except Exception:notes.append("site unreachable")
    if links.get("telegram"):notes.append("telegram linked")
    ok=bool(tw and "/status/" not in tw) or "site up" in notes
    return ok,", ".join(notes),links
# ---------- RUG GUARD: RugCheck ----------
def rug(addr,p,cfg,m,F):
    liq=(p.get("liquidity") or {}).get("usd",0) or 0;pump=p.get("dexId")=="pumpfun"
    mn=cfg.get("pf_min_liq",15000) if pump else cfg["min_liq"]
    if liq<mn:return f"{'curve ' if pump else ''}liquidity ${liq:,.0f} < ${mn:,}"
    ch=(p.get("priceChange") or {}).get("h1",0) or 0
    if ch>cfg["max_1h_change_pct"]:return f"already up {ch:.0f}% in 1h (late)"
    r=get(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report",2.0)
    if not r:return "rugcheck unavailable (fail-closed)"
    if r.get("rugged"):return "rugcheck: rugged"
    if r.get("mintAuthority"):return "mint authority not revoked"
    if r.get("freezeAuthority"):return "freeze authority not revoked"
    skip=set((r.get("knownAccounts") or {}).keys())|{p.get("pairAddress"),m.get("bonding_curve"),m.get("associated_bonding_curve"),m.get("pool_address")}
    for mk in r.get("markets") or []:
        for k in ("pubkey","liquidityA","liquidityB","liquidityAAccount","liquidityBAccount"):
            if isinstance(mk.get(k),str):skip.add(mk[k])
    hs=[h for h in r.get("topHolders") or [] if h.get("owner") not in skip and h.get("address") not in skip]
    if pump and hs and hs[0].get("pct",0)>=30 and not m.get("complete"):hs=hs[1:]   # largest holder on an ungraduated pump.fun coin is assumed to be the bonding curve
    sup=(r.get("token") or {}).get("supply") or 0;dev=100*(r.get("creatorBalance") or 0)/sup if sup else 0
    F.update(dev=dev,creator=r.get("creator"),dev_raw=r.get("creatorBalance") or 0,insiders=r.get("graphInsidersDetected",0) or 0)
    if dev>cfg["max_dev_pct"]:return f"dev wallet {dev:.0f}% - skipped"
    ins=sum(h.get("pct",0) for h in hs if h.get("insider"));F["bundle"]=ins
    if ins>cfg["max_bundle_pct"]:return f"bundles/snipers hold {ins:.0f}% - skipped"
    top10=sum(h.get("pct",0) for h in hs[:10]);F["top10"]=top10
    if top10>cfg["max_top10_pct"]:return f"top10 holders {top10:.0f}% - skipped"
    ct=[t for t in r.get("creatorTokens") or [] if t.get("mint")!=addr];dead=[t for t in ct if (t.get("marketCap") or 0)<5000];F["dev_prior"]=len(ct)
    if len(ct)>=3 and len(dead)>=0.8*len(ct):return f"dev launched {len(ct)} prior tokens, {len(dead)} dead - skipped"
    if F["insiders"]>=5:return f"insider network detected ({F['insiders']})"
    for k in r.get("risks") or []:
        if k.get("level")=="danger":return "rugcheck danger: "+k.get("name","")
    return None
# ---------- score + closed-question gate (run by RISK) ----------
cl01=lambda x:max(0.0,min(1.0,x))
def score(p):
    v=p.get("volume") or {};tx=(p.get("txns") or {}).get("h1",{});liq=(p.get("liquidity") or {}).get("usd",0) or 0
    m5,h1=v.get("m5",0) or 0,v.get("h1",0) or 0
    mom=min(30,30*(m5*12/h1)/2) if h1 else 0
    b,s=tx.get("buys",0),tx.get("sells",0);bs=min(25,25*(b/max(s,1)-0.8)/0.7) if b+s>20 else 0
    lq=min(25,25*max(liq,(p.get("marketCap") or 0)*0.3)/150000)
    age=(time.time()*1000-(p.get("pairCreatedAt") or 0))/3.6e6;ag=20 if 0.1<=age<=24 else 10 if age<=72 else 0
    return round(max(0,mom)+max(0,bs)+lq+ag,1)
def judge(p,F,W,soc_ok):
    liq=(p.get("liquidity") or {}).get("usd",0) or 0;mc=p.get("marketCap") or p.get("fdv") or 0
    tx=(p.get("txns") or {}).get("h1",{});b,sl=tx.get("buys",0),tx.get("sells",0);h1=(p.get("priceChange") or {}).get("h1",0) or 0
    Q=[("Liquidity/market healthy?",cl01(max(liq,mc*0.3)/60000),f"liq ${liq:,.0f}, mcap ${mc:,.0f}"),
       ("Holders spread out?",cl01(1-(F.get("top10",50)+F.get("bundle",0))/55),f"top10 {F.get('top10',0):.0f}%, bundles {F.get('bundle',0):.0f}%"),
       ("Momentum real, not late?",cl01((b/max(b+sl,1)-0.4)/0.25)*cl01(1-max(0,h1-40)/60),f"{b} buys/{sl} sells 1h, {h1:+.0f}%"),
       ("Dev risk low?",cl01(1-F.get("dev",0)/10-F.get("insiders",0)/10),f"dev {F.get('dev',0):.1f}%, {F.get('dev_prior',0)} prior launches"),
       ("Socials legit?",0.75 if soc_ok else 0.35,"links verified" if soc_ok else "weak/missing links")]
    if W and W["n_buys"]+W["n_sells"]>0:Q.append(("Big trades net buying?",cl01(0.5+(W["buy_usd"]-W["sell_usd"])/max(W["buy_usd"]+W["sell_usd"],1)/2),f"${W['buy_usd']:,} in / ${W['sell_usd']:,} out"))
    pr=sum(q[1] for q in Q)/len(Q);return dict(verdict="YES" if pr>=0.6 and min(q[1] for q in Q)>=0.3 else "NO",p=round(pr,2),qs=[dict(q=q,p=round(v,2),why=w) for q,v,w in Q])
# ---------- TRACKER: large trades from GeckoTerminal public trades. Not a profitable-wallet list. ----------
def tracker(p,min_usd):
    pool=p.get("pairAddress");g=get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/trades",2.5,quiet=True) if pool else None
    if not g or "data" not in g:return None
    o=dict(buy_usd=0,sell_usd=0,n_buys=0,n_sells=0)
    for t in g["data"]:
        a=t.get("attributes",{});u=float(a.get("volume_in_usd") or 0)
        if u<min_usd:continue
        k="buy" if a.get("kind")=="buy" else "sell";o[k+"_usd"]+=round(u);o["n_"+k+"s"]+=1
    return o
def dev_balance(cfg,owner,mint):
    try:
        r=S.post(cfg["solana_rpc"],json={"jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner","params":[owner,{"mint":mint},{"encoding":"jsonParsed"}]},timeout=12).json()
        return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in r["result"]["value"])
    except Exception:return None
# ---------- portfolio ----------
def state(c,cfg):
    cash=cfg["start_equity"]
    for p in c.execute("select * from positions"):cash+=-p["cost"]+p["proceeds"]
    op=c.execute("select * from positions where status='open'").fetchall()
    return cash,op,cash+sum(p["remaining"]*p["last"] for p in op)
def sell(c,p,frac,price,reason,cfg):
    q=p["remaining"]*frac;px=price*(1-cfg["slippage"]);gross=q*px;fee=gross*cfg["fee_pct"]
    rem=p["remaining"]-q;closed=rem<=1e-12 or frac>=1
    c.execute("update positions set remaining=?,proceeds=proceeds+?,fees=fees+?,status=?,closed=?,exit_reason=coalesce(exit_reason||'; ','')||? where id=?",
      (0 if closed else rem,gross-fee,fee,"closed" if closed else "open",now() if closed else None,reason,p["id"]))
    pnl=round(p["proceeds"]+gross-fee-p["cost"],2) if closed else round(gross-fee-p["cost"]*frac*(p["remaining"]/p["qty"]),2)
    L("FILL","TRADER",f"SOLD{'' if closed else ' '+format(frac,'.0%')} ${p['symbol']} {'+' if pnl>=0 else '-'}${abs(pnl):.2f} ({reason})",symbol=p["symbol"],pnl=pnl,closed=closed)
    seat("TRADER","done",f"sim sell {frac:.0%} ${p['symbol']} @ {px:.6g}")
def manage(c,cfg,live):
    for p in c.execute("select * from positions where status='open'").fetchall():
        pr=live.get(p["token"])
        if not pr:continue
        peak=max(p["peak"],pr);c.execute("update positions set last=?,peak=? where id=?",(pr,peak,p["id"]))
        p=c.execute("select * from positions where id=?",(p["id"],)).fetchone();r=pr/p["entry"]-1
        age=(dt.datetime.now()-dt.datetime.fromisoformat(p["opened"])).total_seconds()/60
        if p["creator"] and p["dev_bal"]:
            b=dev_balance(cfg,p["creator"],p["token"])
            if b is not None and b<p["dev_bal"]*0.5:seat("RISK","kill",f"${p['symbol']} dev wallet sold - exiting");L("RISK","RISK",f"${p['symbol']} dev wallet sold {100*(1-b/p['dev_bal']):.0f}% - exit");sell(c,p,1,pr,"dev wallet sold",cfg);continue
        if r<=cfg["stop_loss"]:L("RISK","RISK",f"${p['symbol']} stop hit {r:.1%}");sell(c,p,1,pr,f"stop {r:.0%}",cfg);continue
        if p["tp_hit"]>0 and pr<=peak*(1-cfg["trail_pct"]):sell(c,p,1,pr,f"trailing stop {r:+.0%}",cfg);continue
        if age>=cfg["max_hold_min"]:sell(c,p,1,pr,f"time exit {age:.0f}m {r:+.0%}",cfg);continue
        lad=cfg["tp_ladder"]
        if p["tp_hit"]<len(lad) and r>=lad[p["tp_hit"]][0]:
            sell(c,p,lad[p["tp_hit"]][1],pr,f"TP{p['tp_hit']+1} +{r:.0%}",cfg);c.execute("update positions set tp_hit=tp_hit+1 where id=?",(p["id"],))
def day_pnl(c,cfg):
    t=dt.date.today().isoformat();r=c.execute("select equity from equity where ts<? order by ts desc limit 1",(t,)).fetchone()
    base=r[0] if r else cfg["start_equity"];return state(c,cfg)[2]-base,base
def cycle():
    global _conn
    cfg=C();c=db();_conn=c
    seat("SCOUT","working","pulling newest pump.fun launches + DexScreener/GeckoTerminal")
    cands,meta,npf=candidates(cfg)
    op=[p["token"] for p in c.execute("select token from positions where status='open'")]
    seen={r[0] for r in c.execute("select token from signals where ts>?",((dt.datetime.now()-dt.timedelta(hours=6)).isoformat(),))}
    new=[a for a in cands if a not in seen and a not in op]
    pairs=pairs_for(list(set(new)|set(op)))
    live={a:float(p["priceUsd"]) for a,p in pairs.items() if p.get("priceUsd")}
    seat("SCOUT","done",f"{npf} pump.fun coins seen, {len(new)} new candidates");L("SCAN","SCOUT",f"{npf} pump.fun launches, {len(cands)} candidates, {len(new)} new")
    seat("RISK","working",f"managing {len(op)} open positions");manage(c,cfg,live);c.commit()
    seat("RISK","done",f"{len(op)} positions: stops, trail, TP, dev-sell watch")
    nos=[]
    for a in new:
        p=pairs.get(a);m=meta.get(a,{});src=cands[a];sym=(p or {}).get("baseToken",{}).get("symbol") or m.get("symbol") or "?";J=None
        def sig(st,rs,agent,sc=None):
            c.execute("insert into signals(ts,token,symbol,pair,source,liq,score,status,reason,judge,analyst,agent) values(?,?,?,?,?,?,?,?,?,?,?,?)",(now(),a,sym,(p or {}).get("url",""),src,((p or {}).get("liquidity") or {}).get("usd"),sc,st,rs,json.dumps(J) if J else None,round(sc/100,2) if sc is not None else None,agent));c.commit()
        if not p:L("WARN","SCOUT",f"${sym} no DEX pair yet - skipped");sig("killed","no DEX pair data yet","SCOUT");continue
        if p.get("dexId")=="pumpfun" and not ((p.get("liquidity") or {}).get("usd")):
            lq,vs=curve_liq(p,m);p["liquidity"]={"usd":lq,"curve_vsol":vs}
        seat("SOCIAL","working",f"checking ${sym} links");ok,why,links=social(p,m)
        seat("SOCIAL","pass" if ok else "kill",f"${sym}: {why}")
        if ok is None:nos.append(sym);L("WARN","SOCIAL",f"${sym} no X/site links - skipped");sig("killed","no socials","SOCIAL");continue
        seat("RUG GUARD","working",f"RugCheck ${sym}");F={}
        try:k=rug(a,p,cfg,m,F)
        except Exception as e:k=f"rug check error (fail-closed): {e}"
        if k:seat("RUG GUARD","kill",f"${sym} {k}");L("WARN","RUG GUARD",f"${sym} {k}");sig("killed",k,"RUG GUARD");continue
        seat("RUG GUARD","pass",f"${sym} clean: dev {F.get('dev',0):.1f}%, bundles {F.get('bundle',0):.0f}%");L("RISK","RUG GUARD",f"rug check ${sym} - dev {F.get('dev',0):.1f}%, top10 {F.get('top10',0):.0f}%, clean")
        sc=score(p)
        if sc/100<cfg["analyst_gate"]:L("WARN","RISK",f"${sym} score {sc/100:.2f} < {cfg['analyst_gate']} - skipped");sig("killed",f"score {sc/100:.2f} < {cfg['analyst_gate']}","RISK",sc);continue
        seat("TRACKER","working",f"big trades on ${sym}");W=tracker(p,cfg["tracker_min_usd"])
        if W:seat("TRACKER","done",f"${sym}: {W['n_buys']} big buys ${W['buy_usd']:,} / {W['n_sells']} sells ${W['sell_usd']:,}");L("TRACK","TRACKER",f"${sym} {W['n_buys']} buys ≥${cfg['tracker_min_usd']} (${W['buy_usd']:,}) vs {W['n_sells']} sells (${W['sell_usd']:,})")
        J=judge(p,F,W,ok)
        if J["verdict"]!="YES":
            w=min(J["qs"],key=lambda q:q["p"]);seat("RISK","kill",f"${sym} gate NO p={J['p']}");L("WARN","RISK",f"${sym} gate NO p={J['p']} ({w['q']} {w['p']}) - skipped");sig("killed",f"gate NO p={J['p']}: {w['q']}","RISK",sc);continue
        cash,opn,eq=state(c,cfg);dp,base=day_pnl(c,cfg)
        block=("kill switch on" if cfg["kill_switch"] or os.path.exists(f"{DATA}/KILL") else f"daily target hit (+${dp:.2f})" if dp>=cfg["daily_target_pct"]*base else f"daily loss limit hit (${dp:.2f})" if dp<=-cfg["daily_loss_limit_pct"]*base else "max open positions" if len(opn)>=cfg["max_open"] else None)
        if block:seat("RISK","kill",f"${sym} passed but {block}");L("RISK","RISK",f"${sym} passed - not bought: {block}");sig("passed_not_traded",block,"RISK",sc);continue
        size=min(cash,eq*cfg["size_pct"]);mid=float(p["priceUsd"]);px=mid*(1+cfg["slippage"]);fee=size*cfg["fee_pct"];stop=px*(1+cfg["stop_loss"])
        seat("RISK","done",f"${sym}: stop {cfg['stop_loss']:.0%} set before entry, size {cfg['size_pct']:.0%}")
        why=f"gate p={J['p']}, score {sc/100:.2f}, {src}"
        c.execute("insert into positions(token,symbol,pair,opened,entry,qty,cost,peak,last,remaining,fees,status,entry_reason,stop,creator,dev_bal) values(?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",(a,sym,p.get("url"),now(),px,(size-fee)/px,size,px,px,(size-fee)/px,fee,why,stop,F.get("creator"),F.get("dev_raw")))
        seat("TRADER","done",f"sim BUY ${sym} ${size:.2f} @ {px:.6g}");L("EXEC","TRADER",f"BUY ${size:.2f} ${sym} @ ${px:.8g}",symbol=sym,reason=why)
        sig("traded",f"entry ${size:.2f} @ {px:.8g} - {why}","TRADER",sc)
    L("SCAN","SCOUT",f"cycle done: {len(new)} checked, {len(nos)} no socials, equity ${state(c,cfg)[2]:,.2f}")
    cash,opn,eq=state(c,cfg);c.execute("insert into equity values(?,?,?)",(now(),eq,cash));c.commit();_conn=None;write_status()
    for k in SEATS:
        if SEATS[k]["status"]=="working":SEATS[k]["status"]="idle"
def stats():
    c=db();cfg=C();cash,op,eq=state(c,cfg)
    f={r[0]:r[1] for r in c.execute("select status,count(*) from signals group by status")}
    cl=c.execute("select * from positions where status='closed'").fetchall()
    w=sum(1 for p in cl if p["proceeds"]>p["cost"]);rp=sum(p["proceeds"]-p["cost"] for p in cl);dp,base=day_pnl(c,cfg)
    kr=[dict(r) for r in c.execute("select substr(reason,1,instr(reason||' ',' ')+8) k,count(*) n from signals where status='killed' group by k order by n desc limit 8")]
    return dict(ts=now(),start=cfg["start_equity"],equity=round(eq,2),cash=round(cash,2),pnl=round(eq-cfg["start_equity"],2),funnel=dict(signals=sum(f.values()),**f),
      closed=len(cl),wins=w,losses=len(cl)-w,win_rate=round(w/len(cl),3) if cl else None,realized_pnl_after_fees=round(rp,2),fees=round(sum(p["fees"] for p in c.execute("select fees from positions")),2),
      day_pnl=round(dp,2),day_target=round(cfg["daily_target_pct"]*base,2),tasks=TASKS[0]+c.execute("select count(*) from events").fetchone()[0],
      open=[dict(symbol=p["symbol"],entry=p["entry"],last=p["last"],pnl_pct=round(100*(p["last"]/p["entry"]-1),2),opened=p["opened"]) for p in op],top_kill_reasons=kr)
def write_status():
    open(f"{DATA}/status_latest.txt","w").write(json.dumps(stats(),indent=1))
# ---------- dashboard (read-only, password protected) ----------
import hmac,hashlib,secrets,urllib.parse,mimetypes
PW=os.environ.get("DASH_PASSWORD","")
SECRET=hashlib.sha256(("sage:"+PW).encode()).digest()
def tok():
    exp=str(int(time.time())+30*86400);return exp+"."+hmac.new(SECRET,exp.encode(),"sha256").hexdigest()
def valid(t):
    try:exp,sig=t.split(".");return int(exp)>time.time() and hmac.compare_digest(sig,hmac.new(SECRET,exp.encode(),"sha256").hexdigest())
    except Exception:return False
WEB=f"{D}/web";PUBLIC={"/login","/manifest.json","/icon-192.png","/icon-512.png","/apple-touch-icon.png","/sw.js","/favicon.ico","/healthz"}
_fails={}
class H(BaseHTTPRequestHandler):
    def log_message(s,*a):pass
    def authed(s):
        if not PW:return s.client_address[0] in ("127.0.0.1","::1") and not s.headers.get("X-Forwarded-For")
        ck=s.headers.get("Cookie","")
        for p in ck.split(";"):
            k,_,v=p.strip().partition("=")
            if k=="sage" and valid(v):return True
        return False
    def send(s,code,body,ctype="text/html; charset=utf-8",hdr=None):
        s.send_response(code);s.send_header("Content-Type",ctype);s.send_header("Cache-Control","no-store")
        s.send_header("X-Frame-Options","DENY");s.send_header("X-Content-Type-Options","nosniff")
        for k,v in (hdr or {}).items():s.send_header(k,v)
        s.end_headers();s.wfile.write(body if isinstance(body,bytes) else body.encode())
    def static(s,name):
        f=os.path.join(WEB,name.lstrip("/"))
        if not os.path.isfile(f):return s.send(404,"not found","text/plain")
        t=mimetypes.guess_type(f)[0] or "application/octet-stream"
        if f.endswith(".json"):t="application/manifest+json"
        s.send(200,open(f,"rb").read(),t,{"Service-Worker-Allowed":"/"} if f.endswith("sw.js") else None)
    def do_GET(s):
        p=s.path.split("?")[0]
        if p=="/healthz":return s.send(200,"ok","text/plain")
        if p=="/login":return s.static("login.html")
        if p=="/logout":return s.send(302,"",hdr={"Location":"/login","Set-Cookie":"sage=; Path=/; Max-Age=0"})
        if p in PUBLIC:return s.static(p)
        if not s.authed():
            if p=="/api":return s.send(401,'{"error":"auth"}',"application/json")
            return s.send(302,"",hdr={"Location":"/login"})
        if p=="/api":
            c=db();b=json.dumps(dict(stats=stats(),equity=[list(r) for r in c.execute("select ts,equity from equity order by ts")],
              feed=[dict(r) for r in c.execute("select ts,kind,data from events where kind in ('SCAN','EXEC','FILL','WARN','RISK','TRACK') order by id desc limit 80")],
              trades=[dict(r) for r in c.execute("select * from positions order by id desc limit 50")],
              signals=[dict(r) for r in c.execute("select ts,symbol,source,liq,score,analyst,status,reason,pair,judge from signals order by id desc limit 80")],seats=SEATS,agents=AGENTS,poll=C()["poll_sec"],server_now=now()))
            return s.send(200,b,"application/json")
        return s.static("index.html")
    def do_POST(s):
        if s.path!="/login":return s.send(405,"","text/plain")
        ip=s.headers.get("X-Forwarded-For",s.client_address[0]).split(",")[0]
        f=[t for t in _fails.get(ip,[]) if t>time.time()-600];_fails[ip]=f
        if len(f)>=10:return s.send(429,"too many attempts","text/plain")
        n=min(int(s.headers.get("Content-Length",0)),4096);q=urllib.parse.parse_qs(s.rfile.read(n).decode())
        if PW and hmac.compare_digest(q.get("password",[""])[0].encode(),PW.encode()):
            sec="; Secure" if s.headers.get("X-Forwarded-Proto")=="https" else ""
            return s.send(303,"",hdr={"Location":"/","Set-Cookie":f"sage={tok()}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax{sec}"})
        f.append(time.time());time.sleep(1);return s.send(303,"",hdr={"Location":"/login?e=1"})
if __name__=="__main__":
    init();threading.Thread(target=lambda:ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","8787"))),H).serve_forever(),daemon=True).start()
    def st():
        while True:
            try:write_status()
            except Exception:pass
            time.sleep(60)
    threading.Thread(target=st,daemon=True).start()
    while True:
        t=time.time()
        try:cycle()
        except Exception:log("cycle_error",{"tb":traceback.format_exc()[-800:]})
        time.sleep(max(5,C()["poll_sec"]-(time.time()-t)))

"""SAGE BOT - PAPER TRADING ONLY. No wallet, no keys, no signing, no transactions.
Independent asyncio agents connected by queues. Every log line is a real event."""
import asyncio,json,os,time,sqlite3,hmac,hashlib,collections,datetime as dt,random,urllib.parse
import aiohttp,websockets,base64,struct
from aiohttp import web
D=os.path.dirname(os.path.abspath(__file__))
DATA=os.environ.get("DATA_DIR") or D
try:os.makedirs(DATA,exist_ok=True);open(f"{DATA}/.w","w").close()
except Exception:DATA=D
DBF=f"{DATA}/sage.db";CFG=lambda:json.load(open(f"{D}/config.json"))
PW=os.environ.get("DASH_PASSWORD","");START=time.time()
def now():return dt.datetime.now().isoformat(timespec="milliseconds")
# ---------------- storage ----------------
db=sqlite3.connect(DBF,check_same_thread=False);db.row_factory=sqlite3.Row
db.executescript("""create table if not exists events(id integer primary key,ts text,agent text,tag text,msg text,mint text,sym text,pnl real);
create table if not exists positions(id integer primary key,mint text,sym text,opened text,closed text,entry real,qty real,cost real,peak real,last real,remaining real,proceeds real default 0,fees real default 0,status text,reason text,exit_reason text,tp int default 0,creator text);
create table if not exists equity(ts text,equity real);
create table if not exists signals(id integer primary key,ts text,mint text,sym text,stage text,status text,reason text);""");db.commit()
# ---------------- bus / SSE ----------------
SUBS=set()
def push(kind,data):
    m=f"event: {kind}\ndata: {json.dumps(data)}\n\n"
    for q in list(SUBS):
        try:q.put_nowait(m)
        except asyncio.QueueFull:pass
class Agent:
    def __init__(s,name,color):
        s.name=name;s.color=color;s.inbox=asyncio.Queue(5000);s.n_in=0;s.n_out=0;s.done=0;s.task="starting";s.last=0;s.log=collections.deque(maxlen=60)
    def send(s,other,item):
        s.n_out+=1;other.n_in+=1
        try:other.inbox.put_nowait(item)
        except asyncio.QueueFull:pass
    def busy(s,task):s.task=task;s.last=time.time();s.done+=1;push("agent",s.view())
    def say(s,tag,msg,mint=None,sym=None,pnl=None,feed=True):
        ts=now();s.log.appendleft(dict(ts=ts,tag=tag,msg=msg));s.last=time.time()
        if feed:
            db.execute("insert into events(ts,agent,tag,msg,mint,sym,pnl) values(?,?,?,?,?,?,?)",(ts,s.name,tag,msg,mint,sym,pnl));db.commit()
            push("log",dict(ts=ts,agent=s.name,tag=tag,msg=msg))
        push("agent",s.view())
    def view(s):return dict(name=s.name,color=s.color,task=s.task,queue=s.inbox.qsize(),n_in=s.n_in,n_out=s.n_out,done=s.done,last=s.last)
A={n:Agent(n,c) for n,c in [("SCOUT","#0A84FF"),("RUG GUARD","#32D74B"),("SOCIAL","#40C8E0"),("TRACKER","#FF9F0A"),("TRADER","#FF375F"),("RISK","#BF5AF2")]}
TASKS=lambda:sum(a.done for a in A.values())
# ---------------- shared market state ----------------
SOL=[0.0];TOK={}   # mint -> token state
WS=[None];WSQ=asyncio.Queue()
HTTP=[None]
async def ws_send(o):
    if WS[0]:
        try:await WS[0].send(json.dumps(o))
        except Exception:pass
def price_usd(t):return t["mcap_sol"]*SOL[0]/1e9 if SOL[0] else 0
async def jget(url,timeout=8,**kw):
    try:
        async with HTTP[0].get(url,timeout=aiohttp.ClientTimeout(total=timeout),**kw) as r:
            if r.status!=200:return None
            return await r.json(content_type=None)
    except Exception:return None
# ---------------- PumpPortal hub (one connection, as PumpPortal asks) ----------------
async def hub():
    sc=A["SCOUT"]
    while True:
        try:
            async with websockets.connect("wss://pumpportal.fun/api/data",ping_interval=20,max_size=2**22) as ws:
                WS[0]=ws;sc.say("INFO","connected to PumpPortal new-token stream")
                await ws.send(json.dumps({"method":"subscribeNewToken"}))
                async for raw in ws:
                    t0=time.perf_counter();m=json.loads(raw)
                    if m.get("txType")=="create":await on_create(m,t0)
        except Exception as e:
            WS[0]=None;sc.say("INFO",f"stream dropped ({type(e).__name__}), reconnecting");await asyncio.sleep(3)
LAT=collections.deque(maxlen=500)
async def on_create(m,t0):
    sc=A["SCOUT"];c=CFG();mint=m["mint"];dev_pct=100*(m.get("initialBuy") or 0)/1e9
    TOK[mint]=dict(mint=mint,sym=m.get("symbol","?"),name=m.get("name",""),uri=m.get("uri"),creator=m.get("traderPublicKey"),born=time.time(),
        mcap_sol=m.get("marketCapSol",28),vsol=m.get("vSolInBondingCurve",30),dev_pct=dev_pct,vtok=(m.get("vTokensInBondingCurve") or 1.073e9)*1e6,buyers=set(),buys=0,sells=0,early=0.0,dev_sold=False,
        watch=True,rug=None,social=None,tracker=None,peak_mcap=m.get("marketCapSol",28),stage="new")
    DEVS[m.get("traderPublicKey")]+=1
    if m.get("bondingCurveKey"):TOK[mint]["curve"]=m["bondingCurveKey"];await rpc_sub(mint)
    sc.send(A["SOCIAL"],mint);sc.send(A["RUG GUARD"],("fast",mint))
    lat=(time.perf_counter()-t0)*1000;LAT.append(lat)
    sc.busy(f"new ${TOK[mint]['sym']} -> RUG GUARD + SOCIAL in {lat:.1f}ms")
    sc.say("SCAN",f"new pump.fun ${TOK[mint]['sym']} · dev bought {dev_pct:.1f}% · mcap {TOK[mint]['mcap_sol']:.0f} SOL",mint,TOK[mint]['sym'])
    STATS["seen"]+=1
DEVS=collections.Counter();STATS=collections.Counter()
RPCWS=[None];RID=[0];PEND={};SUBID={};MINTSUB={}
async def rpc_send(method,params,tag):
    if not RPCWS[0]:return
    RID[0]+=1;PEND[RID[0]]=tag
    try:await RPCWS[0].send(json.dumps({"jsonrpc":"2.0","id":RID[0],"method":method,"params":params}))
    except Exception:pass
async def rpc_sub(mint):
    t=TOK.get(mint)
    if t and t.get("curve"):await rpc_send("accountSubscribe",[t["curve"],{"encoding":"base64","commitment":"processed"}],("curve",mint))
async def rpc_unsub(mint):
    sid=MINTSUB.pop(mint,None)
    if sid is not None:SUBID.pop(sid,None);await rpc_send("accountUnsubscribe",[sid],("unsub",mint))
async def rpc_hub():
    """Solana public RPC websocket: live bonding-curve account updates (one per trade) + top-wallet log mentions. Free, no key."""
    sc=A["SCOUT"]
    while True:
        try:
            async with websockets.connect(CFG()["rpc_ws"],ping_interval=20,max_size=2**23) as ws:
                RPCWS[0]=ws;PEND.clear();SUBID.clear();MINTSUB.clear();sc.say("INFO","connected to Solana RPC stream (live bonding-curve updates)")
                for mint,t in list(TOK.items()):
                    if t.get("watch"):await rpc_sub(mint)
                for w in TRACKED:await rpc_send("logsSubscribe",[{"mentions":[w]},{"commitment":"confirmed"}],("wallet",w))
                async for raw in ws:
                    d=json.loads(raw)
                    if "id" in d and d["id"] in PEND:
                        kind,key=PEND.pop(d["id"])
                        if kind in("curve","wallet") and isinstance(d.get("result"),int):
                            SUBID[d["result"]]=(kind,key)
                            if kind=="curve":MINTSUB[key]=d["result"]
                        continue
                    p=d.get("params") or {};sub=SUBID.get(p.get("subscription"))
                    if not sub:continue
                    if d.get("method")=="accountNotification":on_curve(sub[1],p["result"]["value"]["data"][0])
                    elif d.get("method")=="logsNotification":
                        v=p["result"]["value"]
                        if not v.get("err"):A["TRACKER"].inbox.put_nowait((sub[1],v["signature"]));A["TRACKER"].n_in+=1
        except Exception as e:
            RPCWS[0]=None;sc.say("INFO",f"RPC stream dropped ({type(e).__name__}), reconnecting");await asyncio.sleep(3)
def on_curve(mint,b64):
    t=TOK.get(mint)
    if not t:return
    try:b=base64.b64decode(b64);vt,vs,rt,rs,ts=struct.unpack_from("<QQQQQ",b,8);done=b[48]==1
    except Exception:return
    STATS["trades"]+=1;vs/=1e9;mc=vs/(vt/1e6)*1e9 if vt else t["mcap_sol"];age=time.time()-t["born"]
    if vs<25 or vs>200 or mc>20*max(t["mcap_sol"],28):t["badcurve"]=True;return   # non-standard curve layout (e.g. non-SOL quote); ignore
    if vs>t["vsol"]+1e-9:
        t["buys"]+=1;t["inflow"]=t.get("inflow",0)+(vs-t["vsol"])
        if age<=CFG()["sniper_window_s"]:t["early"]+=100*max(0,(t.get("vtok",vt)-vt))/1e15
    elif vs<t["vsol"]-1e-9:t["sells"]+=1
    t["vsol"]=vs;t["vtok"]=vt;t["mcap_sol"]=mc;t["peak_mcap"]=max(t["peak_mcap"],mc);t["graduated"]=done
    if t.get("pos"):A["TRADER"].inbox.put_nowait(("tick",mint))
async def dev_balance(owner,mint):
    try:
        async with HTTP[0].post(CFG()["rpc_http"],json={"jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner","params":[owner,{"mint":mint},{"encoding":"jsonParsed"}]},timeout=aiohttp.ClientTimeout(total=8)) as r:
            j=await r.json();return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in j["result"]["value"])
    except Exception:return None
# ---------------- SCOUT momentum watcher (decides which new coins become candidates) ----------------
async def scout_loop():
    sc=A["SCOUT"]
    while True:
        await asyncio.sleep(1);c=CFG();drop=[]
        for mint,t in list(TOK.items()):
            if t["stage"]!="new":continue
            age=time.time()-t["born"]
            if t["dev_sold"]:t["stage"]="dead";sc.say("INFO",f"${t['sym']} dev sold within {age:.0f}s - dropped",mint,t["sym"]);drop.append(mint);continue
            if age>=c["min_watch_s"] and t["buys"]>=c["min_buys"] and t["mcap_sol"]>=c["min_mcap_sol"]:
                t["stage"]="candidate";STATS["candidates"]+=1;sc.busy(f"${t['sym']} candidate")
                sc.say("SCAN",f"${t['sym']} momentum: {t['buys']} buys/{t['sells']} sells, +{t.get('inflow',0):.1f} SOL in, mcap {t['mcap_sol']:.0f} SOL in {age:.0f}s -> candidate",mint,t["sym"])
                sc.send(A["RUG GUARD"],("deep",mint))
            elif age>c["max_watch_s"]:
                t["stage"]="faded";drop.append(mint);sc.busy(f"${t['sym']} faded")
                sc.say("INFO",f"${t['sym']} faded: {t['buys']} buys, peak {t['peak_mcap']:.0f} SOL in {c['max_watch_s']}s - dropped",mint,t["sym"])
        for mint in drop:
            if not TOK[mint].get("pos"):TOK[mint]["watch"]=False;await rpc_unsub(mint)
        for mint in [m for m,t in TOK.items() if t["stage"] in("faded","dead","rejected") and time.time()-t["born"]>900 and not t.get("pos")]:TOK.pop(mint,None)
        if drop:STATS["faded"]+=len(drop)
# ---------------- RUG GUARD (parallel workers) ----------------
async def rug_worker(i):
    g=A["RUG GUARD"]
    while True:
        kind,mint=await g.inbox.get();t=TOK.get(mint);c=CFG()
        if not t:continue
        if kind=="fast":
            g.busy(f"w{i}: dev/creator check ${t['sym']}")
            if t["dev_pct"]>c["max_dev_pct"]:t["rug"]=f"dev bought {t['dev_pct']:.0f}%";t["stage"]="rejected";g.say("WARN",f"${t['sym']} dev wallet {t['dev_pct']:.0f}% - skipped",mint,t["sym"]);rej(t,"RUG GUARD",t["rug"])
            elif DEVS[t["creator"]]>c["max_dev_launches_session"]:t["rug"]=f"dev launched {DEVS[t['creator']]} tokens this session";t["stage"]="rejected";g.say("WARN",f"${t['sym']} serial launcher ({DEVS[t['creator']]} coins seen from this dev) - skipped",mint,t["sym"]);rej(t,"RUG GUARD",t["rug"])
            continue
        g.busy(f"w{i}: RugCheck + snipers ${t['sym']}")
        if t["early"]>c["max_sniper_pct"]:t["rug"]=f"snipers {t['early']:.0f}%";t["stage"]="rejected";g.say("WARN",f"${t['sym']} first-{c['sniper_window_s']}s snipers hold {t['early']:.0f}% - skipped",mint,t["sym"]);rej(t,"RUG GUARD",t["rug"]);continue
        db_=await dev_balance(t["creator"],mint)
        if db_ is not None and t["dev_pct"]>0.05 and db_<1e7*t["dev_pct"]*0.5:t["dev_sold"]=True
        if db_ is not None:t["dev_now_pct"]=db_/1e7
        r=await jget(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report",10)
        why=None
        if r:
            if r.get("mintAuthority"):why="mint authority not revoked"
            elif r.get("freezeAuthority"):why="freeze authority not revoked"
            else:
                ct=[x for x in r.get("creatorTokens") or [] if x.get("mint")!=mint];dead=[x for x in ct if (x.get("marketCap") or 0)<5000]
                if len(ct)>=3 and len(dead)>=0.8*len(ct):why=f"dev has {len(ct)} prior tokens, {len(dead)} dead"
                elif (r.get("graphInsidersDetected") or 0)>=5:why=f"insider network ({r['graphInsidersDetected']})"
        else:why=None if c["rugcheck_optional_for_new"] else "RugCheck unavailable"
        if why:t["rug"]=why;t["stage"]="rejected";g.say("WARN",f"${t['sym']} {why} - skipped",mint,t["sym"]);rej(t,"RUG GUARD",why);continue
        t["rug"]="ok";g.say("INFO",f"rug check ${t['sym']} clean: dev {t.get('dev_now_pct',t['dev_pct']):.1f}% (on-chain), snipers {t['early']:.0f}%"+("" if r else ", RugCheck not indexed yet"),mint,t["sym"])
        g.send(A["TRADER"],("eval",mint))
def rej(t,agent,why):
    STATS["rejected"]+=1;db.execute("insert into signals(ts,mint,sym,stage,status,reason) values(?,?,?,?,?,?)",(now(),t["mint"],t["sym"],agent,"killed",why));db.commit()
# ---------------- SOCIAL (parallel workers) ----------------
async def social_worker(i):
    s=A["SOCIAL"]
    while True:
        mint=await s.inbox.get();t=TOK.get(mint)
        if not t or not t.get("uri"):continue
        s.busy(f"w{i}: metadata ${t['sym']}")
        md=await jget(t["uri"].replace("https://ipfs.io/ipfs/",CFG()["ipfs_gateway"]),6) or {}
        L={k:md.get(k) for k in ("twitter","website","telegram") if md.get(k)}
        notes=[];tw=L.get("twitter") or ""
        if "/status/" in tw:notes.append("X link is a tweet")
        elif tw:notes.append("X account")
        if L.get("website") and "x.com" not in L["website"]:
            try:
                async with HTTP[0].head(L["website"],timeout=aiohttp.ClientTimeout(total=5),allow_redirects=True) as r:notes.append("site up" if r.status<400 else f"site {r.status}")
            except Exception:notes.append("site down")
        if L.get("telegram"):notes.append("telegram")
        t["social"]=", ".join(notes) or "none";t["social_ok"]=bool(tw and "/status/" not in tw) or "site up" in notes
        if t.get("stage")=="candidate":s.say("INFO",f"${t['sym']} socials: {t['social']}",mint,t["sym"])
        if t.get("pending_social"):s.send(A["TRADER"],("eval",mint))
# ---------------- TRACKER: keyless fomo-api top-10 realized-PnL wallets ----------------
TRACKED={}
async def tracker():
    tr=A["TRACKER"]
    async def refresh():
        d=await jget("https://api.fomo-api.com/v1/leaderboard?window=7d&board=solana&limit=10",15)
        rows=(d or {}).get("data") or []
        if rows:
            new={r["address"]:(r.get("handle") or r["address"][:6]) for r in rows};add=[k for k in new if k not in TRACKED];TRACKED.update(new)
            for w in add:await rpc_send("logsSubscribe",[{"mentions":[w]},{"commitment":"confirmed"}],("wallet",w))
            tr.busy("watching top-10 wallets");tr.say("INFO",f"watching {len(new)} top wallets on-chain (fomo-api.com 7d realized PnL, keyless top 10): "+", ".join(list(new.values())[:5])+"…")
        else:tr.say("INFO","fomo-api.com leaderboard unavailable; retrying in 30 min")
    while not RPCWS[0]:await asyncio.sleep(1)
    await refresh();last=time.time()
    while True:
        try:w,sig=await asyncio.wait_for(tr.inbox.get(),60)
        except asyncio.TimeoutError:w=None
        if time.time()-last>1800:await refresh();last=time.time()
        if not w:continue
        who=TRACKED.get(w,w[:6]);tr.busy(f"decode {who} tx")
        try:
            async with HTTP[0].post(CFG()["rpc_http"],json={"jsonrpc":"2.0","id":1,"method":"getTransaction","params":[sig,{"encoding":"jsonParsed","maxSupportedTransactionVersion":0,"commitment":"confirmed"}]},timeout=aiohttp.ClientTimeout(total=10)) as r:
                tx=(await r.json()).get("result")
        except Exception:tx=None
        if not tx:continue
        meta=tx.get("meta") or {};chg=collections.defaultdict(float)
        for side,sgn in (("preTokenBalances",-1),("postTokenBalances",1)):
            for bl in meta.get(side) or []:
                if bl.get("owner")==w:chg[bl["mint"]]+=sgn*float(bl["uiTokenAmount"]["uiAmount"] or 0)
        keys=[k["pubkey"] if isinstance(k,dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        sol=0
        if w in keys:i=keys.index(w);sol=(meta["postBalances"][i]-meta["preBalances"][i])/1e9
        WS_={"So11111111111111111111111111111111111111112","EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}
        for mint,dq in chg.items():
            if mint in WS_ or abs(dq)<1e-9:continue
            sym=TOK.get(mint,{}).get("sym") or mint[:4]+"…"+mint[-4:]
            tr.say("INFO",f"top trader {who} {'bought' if dq>0 else 'sold'} {sym} ({abs(sol):.2f} SOL)",mint)
            t=TOK.get(mint)
            if t and dq>0:t["tracker"]=who
# ---------------- RISK supervisor ----------------
def portfolio():
    c=CFG();cash=c["start_equity"]
    for p in db.execute("select cost,proceeds from positions"):cash+=-p["cost"]+p["proceeds"]
    op=db.execute("select * from positions where status='open'").fetchall()
    return cash,op,cash+sum(p["remaining"]*p["last"] for p in op)
def day_pnl():
    c=CFG();t=dt.date.today().isoformat();r=db.execute("select equity from equity where ts<? order by ts desc limit 1",(t,)).fetchone()
    base=r[0] if r else c["start_equity"];return portfolio()[2]-base,base
def risk_check(t):
    c=CFG();cash,op,eq=portfolio();dp,base=day_pnl()
    if c["kill_switch"] or os.path.exists(f"{DATA}/KILL"):return "kill switch on"
    if dp>=c["daily_target_pct"]*base:return f"daily target reached (+${dp:.2f})"
    if dp<=-c["daily_loss_limit_pct"]*base:return f"daily loss limit hit (${dp:.2f})"
    if len(op)>=c["max_open"]:return f"max {c['max_open']} open positions"
    if any(p["mint"]==t["mint"] for p in op):return "already holding"
    return None
async def risk_loop():
    r=A["RISK"];last_eq=0
    while True:
        try:req=await asyncio.wait_for(r.inbox.get(),1)
        except asyncio.TimeoutError:req=None
        if time.time()-last_eq>=15:
            cash,op,eq=portfolio();db.execute("insert into equity values(?,?)",(now(),eq));db.commit();last_eq=time.time();push("equity",[now(),eq])
        if not req:continue
        mint,fut=req;t=TOK.get(mint);v=risk_check(t) if t else "token gone"
        r.busy(f"{'veto' if v else 'approve'} ${t['sym'] if t else '?'}")
        if v:r.say("WARN",f"${t['sym'] if t else '?'} passed gates - RISK veto: {v}",mint)
        fut.set_result(v)
# ---------------- TRADER ----------------
async def trader():
    tr=A["TRADER"]
    while True:
        kind,mint=await tr.inbox.get();t=TOK.get(mint);c=CFG()
        if not t:continue
        if kind=="tick":await manage(t);continue
        tr.busy(f"evaluate ${t['sym']}")
        if t.get("social") is None and not t.get("pending_social"):t["pending_social"]=True;continue
        if c["require_socials"] and not t.get("social_ok"):t["stage"]="rejected";tr.say("WARN",f"${t['sym']} no real X/site ({t.get('social')}) - skipped",mint,t["sym"]);rej(t,"SOCIAL","weak socials");continue
        age=time.time()-t["born"];up=t["mcap_sol"]/max(t.get("cand_mcap") or t["mcap_sol"],1)
        if t.get("graduated"):t["stage"]="rejected";tr.say("WARN",f"${t['sym']} already graduated - skipped",mint,t["sym"]);rej(t,"TRADER","graduated");continue
        if t["dev_sold"]:t["stage"]="rejected";tr.say("WARN",f"${t['sym']} dev sold - skipped",mint,t["sym"]);rej(t,"TRADER","dev sold");continue
        if t["mcap_sol"]>c["max_entry_mcap_sol"]:t["stage"]="rejected";tr.say("WARN",f"${t['sym']} too late: mcap {t['mcap_sol']:.0f} SOL > {c['max_entry_mcap_sol']}",mint,t["sym"]);rej(t,"TRADER","late");continue
        fut=asyncio.get_running_loop().create_future();tr.send(A["RISK"],(mint,fut));v=await fut
        if v:t["stage"]="vetoed";rej(t,"RISK",v);continue
        cash,op,eq=portfolio();size=min(cash,eq*c["size_pct"]);px=price_usd(t)*(1+c["slippage"]);fee=size*c["fee_pct"]
        if px<=0:continue
        qty=(size-fee)/px
        cur=db.execute("insert into positions(mint,sym,opened,entry,qty,cost,peak,last,remaining,fees,status,reason,creator) values(?,?,?,?,?,?,?,?,?,?,'open',?,?)",(mint,t["sym"],now(),px,qty,size,px,px,qty,fee,f"{t['buys']} buys, +{t.get('inflow',0):.1f} SOL in, mcap {t['mcap_sol']:.0f} SOL, socials: {t.get('social')}"+(f", top trader {t['tracker']} in" if t.get("tracker") else ""),t["creator"]));db.commit()
        t["pos"]=cur.lastrowid;t["stage"]="held";STATS["traded"]+=1
        tr.say("BUY",f"BUY ${size:.2f} ${t['sym']} @ ${px:.8f} ({age:.0f}s old, mcap {t['mcap_sol']:.0f} SOL)",mint,t["sym"])
async def manage(t):
    c=CFG();tr=A["TRADER"];p=db.execute("select * from positions where id=?",(t["pos"],)).fetchone()
    if not p or p["status"]!="open":return
    pr=price_usd(t)
    if pr<=0:return
    peak=max(p["peak"],pr);db.execute("update positions set last=?,peak=? where id=?",(pr,peak,p["id"]));r=pr/p["entry"]-1
    age=(dt.datetime.now()-dt.datetime.fromisoformat(p["opened"])).total_seconds()
    why=None;frac=1
    if t["dev_sold"]:why="dev wallet sold"
    elif r<=c["stop_loss"]:why=f"stop {r:.0%}"
    elif p["tp"]>0 and pr<=peak*(1-c["trail_pct"]):why=f"trailing stop {r:+.0%}"
    elif age>=c["max_hold_s"]:why=f"time exit {age/60:.0f}m {r:+.0%}"
    elif p["tp"]<len(c["tp_ladder"]) and r>=c["tp_ladder"][p["tp"]][0]:why=f"TP{p['tp']+1} +{r:.0%}";frac=c["tp_ladder"][p["tp"]][1];db.execute("update positions set tp=tp+1 where id=?",(p["id"],))
    if not why:db.commit();return
    tr.busy(f"exit ${p['sym']}: {why}")
    q=p["remaining"]*frac;px=pr*(1-c["slippage"]);gross=q*px;fee=gross*c["fee_pct"];rem=p["remaining"]-q;closed=frac>=1 or rem<=1e-9
    db.execute("update positions set remaining=?,proceeds=proceeds+?,fees=fees+?,status=?,closed=?,exit_reason=coalesce(exit_reason||'; ','')||? where id=?",(0 if closed else rem,gross-fee,fee,"closed" if closed else "open",now() if closed else None,why,p["id"]));db.commit()
    pnl=(p["proceeds"]+gross-fee-p["cost"]) if closed else gross-fee-p["cost"]*frac*p["remaining"]/p["qty"]
    tr.say("SELL",f"SOLD{'' if closed else ' '+format(frac,'.0%')} ${p['sym']} {'+' if pnl>=0 else '-'}${abs(pnl):.2f} ({why})",t["mint"],p["sym"],round(pnl,2))
    if closed:
        t["pos"]=None;t["watch"]=False;await rpc_unsub(t["mint"])
async def time_exits():
    while True:
        await asyncio.sleep(5)
        for t in [t for t in TOK.values() if t.get("pos")]:
            if t["dev_pct"]>0.05 and time.time()-t.get("devchk",0)>10:
                t["devchk"]=time.time();b=await dev_balance(t["creator"],t["mint"])
                if b is not None and b<1e7*t["dev_pct"]*0.5 and not t["dev_sold"]:t["dev_sold"]=True;A["RISK"].busy(f"dev sold ${t['sym']}");A["RISK"].say("WARN",f"${t['sym']} dev wallet sold - forcing exit",t["mint"],t["sym"])
            await manage(t)
        for p in db.execute("select * from positions where status='open'").fetchall():
            if p["mint"] not in TOK:   # restart: position without live state -> close at last known price
                db.execute("update positions set status='closed',closed=?,proceeds=proceeds+remaining*last*0.98,remaining=0,exit_reason='closed after restart (no live price)' where id=?",(now(),p["id"]));db.commit()
                A["TRADER"].say("SELL",f"SOLD ${p['sym']} at last price (engine restarted, no live stream for it)",p["mint"],p["sym"])
async def sol_price():
    while True:
        d=await jget("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd")
        v=(d or {}).get("solana",{}).get("usd")
        if not v:
            d=await jget("https://api.dexscreener.com/latest/dex/pairs/solana/58oqchx4ywmvkdwllzzbi4chocc2fqcuwbkwmihlyqo2");v=float(((d or {}).get("pair") or {}).get("priceUsd") or 0) or None
        if v:SOL[0]=float(v)
        await asyncio.sleep(60)
# ---------------- stats / API ----------------
def stats():
    c=CFG();cash,op,eq=portfolio();cl=db.execute("select cost,proceeds from positions where status='closed'").fetchall()
    w=sum(1 for p in cl if p["proceeds"]>p["cost"]);dp,base=day_pnl()
    return dict(start=c["start_equity"],equity=round(eq,2),pnl=round(eq-c["start_equity"],2),wins=w,losses=len(cl)-w,open=len(op),day_pnl=round(dp,2),day_target=round(c["daily_target_pct"]*base,2),
        tasks=TASKS(),resolved=STATS["rejected"]+STATS["faded"]+STATS["traded"],seen=STATS["seen"],trades=STATS["trades"],candidates=STATS["candidates"],uptime=int(time.time()-START),sol=SOL[0],
        rpc=bool(RPCWS[0]),lat_ms=round(sorted(LAT)[len(LAT)//2],2) if LAT else None,ws=bool(WS[0]),watching=sum(1 for t in TOK.values() if t.get("watch")),sub=c["subscription"])
def snapshot():
    return dict(stats=stats(),agents=[a.view() for a in A.values()],server_now=time.time(),
        equity=[list(r) for r in db.execute("select ts,equity from (select ts,equity from equity order by ts desc limit 1500) order by ts")],
        log=[dict(r) for r in db.execute("select ts,agent,tag,msg from events order by id desc limit 120")])
SECRET=hashlib.sha256(("sage:"+PW).encode()).digest()
def tok():e=str(int(time.time())+30*86400);return e+"."+hmac.new(SECRET,e.encode(),"sha256").hexdigest()
def valid(t):
    try:e,s=t.split(".");return int(e)>time.time() and hmac.compare_digest(s,hmac.new(SECRET,e.encode(),"sha256").hexdigest())
    except Exception:return False
PUBLIC={"/login","/manifest.json","/icon-192.png","/icon-512.png","/apple-touch-icon.png","/sw.js","/favicon.ico","/healthz"}
FAILS=collections.defaultdict(list)
@web.middleware
async def auth(req,handler):
    if req.path in PUBLIC or req.path=="/logout":return await handler(req)
    ok=valid(req.cookies.get("sage","")) if PW else req.remote in("127.0.0.1","::1") and "X-Forwarded-For" not in req.headers
    if not ok:
        if req.path.startswith("/api"):return web.json_response({"error":"auth"},status=401)
        raise web.HTTPFound("/login")
    return await handler(req)
def static(name,ct=None):
    async def h(req):
        r=web.FileResponse(f"{D}/web/{name}");r.headers["Cache-Control"]="no-cache"
        if ct:r.content_type=ct
        return r
    return h
async def login_post(req):
    ip=req.headers.get("X-Forwarded-For",req.remote or "").split(",")[0];FAILS[ip]=[x for x in FAILS[ip] if x>time.time()-600]
    if len(FAILS[ip])>=10:return web.Response(status=429,text="too many attempts")
    f=await req.post()
    if PW and hmac.compare_digest(str(f.get("password","")).encode(),PW.encode()):
        r=web.HTTPSeeOther("/");r.set_cookie("sage",tok(),max_age=2592000,httponly=True,samesite="Lax",secure=req.headers.get("X-Forwarded-Proto")=="https");raise r
    FAILS[ip].append(time.time());await asyncio.sleep(1);raise web.HTTPSeeOther("/login?e=1")
async def logout(req):
    r=web.HTTPFound("/login");r.del_cookie("sage");raise r
async def api_state(req):return web.json_response(snapshot(),headers={"Cache-Control":"no-store"})
async def api_agent(req):
    a=A.get(req.match_info["name"].upper().replace("-"," "))
    if not a:raise web.HTTPNotFound()
    return web.json_response(dict(a.view(),log=list(a.log)),headers={"Cache-Control":"no-store"})
async def api_stream(req):
    r=web.StreamResponse(headers={"Content-Type":"text/event-stream","Cache-Control":"no-store","X-Accel-Buffering":"no"});await r.prepare(req)
    q=asyncio.Queue(2000);SUBS.add(q)
    try:
        await r.write(f"event: hello\ndata: {json.dumps(snapshot())}\n\n".encode());last=time.time()
        while True:
            try:m=await asyncio.wait_for(q.get(),2)
            except asyncio.TimeoutError:m=None
            if m:await r.write(m.encode())
            if time.time()-last>=2:await r.write(f"event: stats\ndata: {json.dumps(dict(stats=stats(),server_now=time.time()))}\n\n".encode());last=time.time()
    except (ConnectionResetError,asyncio.CancelledError,Exception):pass
    finally:SUBS.discard(q)
    return r
async def main():
    HTTP[0]=aiohttp.ClientSession(headers={"User-Agent":"Mozilla/5.0 (sage paper terminal)"})
    app=web.Application(middlewares=[auth])
    app.router.add_get("/healthz",lambda r:web.Response(text="ok"))
    app.router.add_get("/login",static("login.html"));app.router.add_post("/login",login_post);app.router.add_get("/logout",logout)
    for n in ("manifest.json","icon-192.png","icon-512.png","apple-touch-icon.png","favicon.ico"):app.router.add_get("/"+n,static(n))
    app.router.add_get("/sw.js",static("sw.js","application/javascript"))
    app.router.add_get("/",static("index.html"));app.router.add_get("/api/state",api_state);app.router.add_get("/api/stream",api_stream);app.router.add_get("/api/agent/{name}",api_agent)
    run=web.AppRunner(app);await run.setup();await web.TCPSite(run,"0.0.0.0",int(os.environ.get("PORT","8787"))).start()
    c=CFG();tasks=[hub(),rpc_hub(),scout_loop(),tracker(),risk_loop(),trader(),time_exits(),sol_price()]+[rug_worker(i) for i in range(c["rug_workers"])]+[social_worker(i) for i in range(c["social_workers"])]
    await asyncio.gather(*tasks)
if __name__=="__main__":asyncio.run(main())

"""SAGE TERMINAL v5 - PAPER TRADING ONLY. No wallet, no keys, no signing, no transactions. Ever.
A population of paper bots trades one shared live pump.fun feed. Every log line is a real event.
Feeds (free, keyless): PumpPortal new-token websocket; Solana public RPC getMultipleAccounts (1s curve polling);
Solana RPC logsSubscribe for fomo-api.com top-10 wallets; RugCheck; CoinGecko SOL price."""
import asyncio,json,os,time,sqlite3,hmac,hashlib,collections,datetime as dt,random,base64,struct,math,sys
import aiohttp,websockets
from aiohttp import web
D=os.path.dirname(os.path.abspath(__file__));sys.path.insert(0,D)
import strat
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
create table if not exists signals(id integer primary key,ts text,mint text,sym text,stage text,status text,reason text);
create table if not exists bots(id integer primary key,name text,gen int,parents text,style text,genome text,born text,died text,status text,cash real,start_cash real,epoch_start real,note text);
create table if not exists bot_pos(id integer primary key,bot int,mint text,sym text,curve text,opened text,opened_ts real,closed text,entry_mcap real,peak_mcap real,tok real,tok0 real,cost real,proceeds real default 0,tp_done int default 0,status text,reason text,exit_reason text,sol_px real);
create table if not exists evolutions(id integer primary key,ts text,epoch int,ranking text,removed text,added text);
create table if not exists kv(k text primary key,v text);
create table if not exists ticks(mint text primary key,sym text,born real,creator text,dev_pct real,social int,series text);""");db.commit()
def kv(k,d=None):
    r=db.execute("select v from kv where k=?",(k,)).fetchone();return json.loads(r[0]) if r else d
def kvset(k,v):db.execute("insert or replace into kv values(?,?)",(k,json.dumps(v)));db.commit()
# ---------------- bus / SSE ----------------
SUBS=set()
def push(kind,data):
    m=f"event: {kind}\ndata: {json.dumps(data)}\n\n"
    for q in list(SUBS):
        try:q.put_nowait(m)
        except asyncio.QueueFull:pass
class Agent:
    def __init__(s,name,color,role):
        s.name=name;s.color=color;s.role=role;s.line="starting";s.icon="·";s.total=0;s.last=0;s.log=collections.deque(maxlen=80);s.dirty=True
    def live(s,icon,line):s.icon=icon;s.line=line;s.last=time.time();s.dirty=True   # funnel line (overwritten many times a second)
    def say(s,tag,msg,mint=None,sym=None,pnl=None,head=False):
        ts=now();s.log.appendleft(dict(ts=ts,tag=tag,msg=msg));s.last=time.time()
        db.execute("insert into events(ts,agent,tag,msg,mint,sym,pnl) values(?,?,?,?,?,?,?)",(ts,s.name,tag,msg,mint,sym,pnl))
        push("log",dict(ts=ts,agent=s.name,tag=tag,msg=msg))
        if head:HEAD[:]=[msg,f"{s.name} · {dt.datetime.now().strftime('%H:%M:%S')}"];push("head",HEAD)
    def view(s):return dict(name=s.name,color=s.color,role=s.role,icon=s.icon,line=s.line,total=s.total,last=s.last)
A={n:Agent(n,c,r) for n,c,r in [("SCOUT","#3399FF","finds every pump.fun launch"),("FILTER","#FF4444","kills rugs & traps"),("ANALYST","#A020F0","scores coins for each bot"),("EXECUTOR","#00FF41","paper fills on the curve"),("RISK","#FF3399","stops, limits, evolution")]}
HEAD=["engine starting","SAGE v5"];HOPS=collections.Counter()
def hop(a,b):HOPS[(a,b)]+=1
KILLS=collections.Counter(kv("kills",{}) or {})
STATS=collections.Counter(kv("stats",{}) or {})
def kill(reason):KILLS[reason]+=1
# ---------------- shared market state ----------------
SOL=[0.0];COINS={};CURVE={}     # mint -> strat.Coin ; mint -> curve address
META={}   # mint -> dict(sym, uri, creator, social, stage, rug, last_reason, series, killed)
HTTP=[None];WS=[None]
async def jget(url,timeout=8):
    try:
        async with HTTP[0].get(url,timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status!=200:return None
            return await r.json(content_type=None)
    except Exception:return None
RPCI=[0];RPCBAD=collections.Counter()
async def rpc(method,params,timeout=8):
    """Free keyless RPC pool with failover (PublicNode first, Solana public RPC second)."""
    pool=CFG()["rpc_http_pool"]
    for k in range(len(pool)):
        u=pool[(RPCI[0]+k)%len(pool)]
        try:
            async with HTTP[0].post(u,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status==200:
                    j=await r.json(content_type=None)
                    if "result" in j:RPCI[0]=(RPCI[0]+k)%len(pool);return j["result"]
                RPCBAD[u]+=1
        except Exception:RPCBAD[u]+=1
    return "429"
def usd(sol):return sol*SOL[0]
def mcap_usd(c):return c.mcap*SOL[0]
WD=dict(create=time.time(),poll=time.time(),loop=time.time())
# ---------------- SCOUT: PumpPortal new tokens ----------------
async def hub():
    sc=A["SCOUT"]
    while True:
        try:
            async with websockets.connect("wss://pumpportal.fun/api/data",ping_interval=20,ping_timeout=20,max_size=2**22) as ws:
                WS[0]=ws;sc.say("INFO","connected to PumpPortal new-token stream");WD["create"]=time.time()
                await ws.send(json.dumps({"method":"subscribeNewToken"}))
                async for raw in ws:
                    m=json.loads(raw)
                    if m.get("txType")=="create":on_create(m)
        except Exception as e:
            WS[0]=None;sc.say("INFO",f"PumpPortal stream dropped ({type(e).__name__}), reconnecting");await asyncio.sleep(3)
def on_create(m):
    sc=A["SCOUT"];mint=m["mint"];ts=time.time();WD["create"]=ts
    dev_pct=100*(m.get("initialBuy") or 0)/1e9;sym=m.get("symbol","?")[:12]
    vs=m.get("vSolInBondingCurve") or 30;vt=m.get("vTokensInBondingCurve") or 1.073e9
    c=strat.Coin(mint,sym,ts,m.get("traderPublicKey"),dev_pct,vs/vt*1e9);c.vsol=vs;c.vtok=vt
    COINS[mint]=c;META[mint]=dict(sym=sym,uri=m.get("uri"),stage="new",series=[(round(ts,1),round(c.mcap,2))],last_reason=collections.Counter())
    if m.get("bondingCurveKey"):CURVE[mint]=m["bondingCurveKey"]
    STATS["seen"]+=1;A["SCOUT"].total=STATS["seen"];hop("SCOUT","FILTER")
    sc.live("◎",f"new ${sym} · dev {dev_pct:.1f}% · {c.mcap:.0f} SOL mc")
    r=strat.hard_kill(c,ts)
    if r:retire_kill(mint,r)
    else:asyncio.create_task(social(mint))
def retire_kill(mint,reason,agent="FILTER"):
    m=META.get(mint)
    if not m or m.get("killed"):return
    m["killed"]=reason;m["stage"]="killed";kill(reason);STATS["killed"]+=1;A["FILTER"].total=STATS["killed"]
    A[agent].live("✕",f"${m['sym']} {reason}");db.execute("insert into signals(ts,mint,sym,stage,status,reason) values(?,?,?,?,?,?)",(now(),mint,m["sym"],agent,"killed",reason))
# ---------------- SCOUT: 1s curve polling (getMultipleAccounts, 100 per call) ----------------
async def poller():
    sc=A["SCOUT"];backoff=0
    while True:
        t0=time.time()
        live=[m for m in COINS if m in CURVE and not META[m].get("killed") or holders(m)]
        n_=int(t0)
        live=[m for m in live if m in CURVE and (holders(m) or t0-COINS[m].born<120 or t0-COINS[m].last_ts<60 or n_%5==0)]
        ok=True
        for i in range(0,len(live),100):
            ch=live[i:i+100];res=await rpc("getMultipleAccounts",[[CURVE[m] for m in ch],{"encoding":"base64","commitment":"processed"}])
            if res=="429" or res is None:ok=False;continue
            ts=time.time()
            for m,acc in zip(ch,res.get("value") or []):
                if acc:on_curve(m,acc["data"][0],ts)
        if ok and live:WD["poll"]=time.time();backoff=0
        elif not live:WD["poll"]=time.time()
        else:
            backoff=min(backoff+1,5)
            if backoff==3:sc.say("INFO","RPC curve poll throttled; backing off")
        await asyncio.sleep(max(0.15,1.0-(time.time()-t0))+backoff*0.5)
def on_curve(mint,b64,ts):
    c=COINS.get(mint);m=META.get(mint)
    if not c:return
    try:b=base64.b64decode(b64);vt,vs,rt,rs,sup=struct.unpack_from("<QQQQQ",b,8);done=b[48]==1
    except Exception:return
    if vt<=0 or vs<=0:return
    vs/=1e9;vt/=1e6
    if abs(vs-c.vsol)<1e-9 and not done:return
    prev=c.mcap;d=vs-c.vsol;c.tick(ts,vs,vt);c.graduated=c.graduated or done;STATS["updates"]+=1;hop("SCOUT","CORE")
    if len(m["series"])<3000:m["series"].append((round(ts,1),round(c.mcap,3)))
    if abs(d)>0.001:A["SCOUT"].live("↗" if d>0 else "↘",f"${m['sym']} {'+' if d>0 else '−'}{abs(d):.2f} SOL · {c.mcap:.0f} SOL mc")
    if not m.get("killed"):
        r=strat.hard_kill(c,ts)
        if r:retire_kill(mint,r)
    EXEC.fill_pending(mint,ts)
    ANALYST(mint,ts)
    RISKMGR(mint,ts)
# ---------------- FILTER: socials (penalty info) + RugCheck + dev balance ----------------
async def social(mint):
    m=META.get(mint)
    if not m or not m.get("uri"):return
    md=await jget(m["uri"],6) or {}
    tw=md.get("twitter") or "";site=md.get("website") or ""
    ok=bool(tw and "/status/" not in tw) or bool(site)
    c=COINS.get(mint)
    if c:c.social_ok=ok
    m["social"]="X account" if tw and "/status/" not in tw else ("X tweet link" if tw else ("site" if site else "none"))
async def dev_balance(owner,mint):
    r=await rpc("getTokenAccountsByOwner",[owner,{"mint":mint},{"encoding":"jsonParsed"}])
    if not isinstance(r,dict):return None
    try:return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in r["value"])
    except Exception:return None
async def deep_check(mint):
    """Called when the first bot wants a coin: on-chain dev balance + RugCheck authorities."""
    c=COINS.get(mint);m=META.get(mint)
    if not c or m.get("deep"):return m.get("deep")
    m["deep"]="pending";f=A["FILTER"];f.live("⌕",f"RugCheck + dev wallet ${m['sym']}")
    if c.dev_pct>0.05:
        b=await dev_balance(c.creator,mint)
        if b is not None and b<1e7*c.dev_pct*0.5:c.dev_sold=True
    r=await jget(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report",6)
    if r and r.get("mintAuthority"):c.hard_kill="mint authority live"
    elif r and r.get("freezeAuthority"):c.hard_kill="freeze authority live"
    why=strat.hard_kill(c,time.time())
    if why:retire_kill(mint,why);m["deep"]="kill";f.say("KILL",f"${m['sym']} {why}",mint,m["sym"]);return "kill"
    m["deep"]="ok";f.live("✓",f"${m['sym']} clean: dev {c.dev_pct:.1f}%, snipers {c.early_pct:.0f}%");hop("FILTER","ANALYST");return "ok"
async def dev_watch():
    """Every 10s: dev balance for coins any bot holds (dev sell = forced exit)."""
    while True:
        await asyncio.sleep(10)
        for mint in [m for m in COINS if holders(m)]:
            c=COINS[mint]
            if c.dev_pct>0.05 and not c.dev_sold:
                b=await dev_balance(c.creator,mint)
                if b is not None and b<1e7*c.dev_pct*0.5:c.dev_sold=True;A["RISK"].say("WARN",f"${c.sym} dev wallet sold - forcing exits",mint,c.sym,head=True)
# ---------------- TRACKER (part of SCOUT): top wallets via logsSubscribe ----------------
TRACKED={}
async def wallets():
    sc=A["SCOUT"]
    while True:
        d=await jget("https://api.fomo-api.com/v1/leaderboard?window=7d&board=solana&limit=10",15)
        rows=(d or {}).get("data") or []
        if rows:TRACKED.clear();TRACKED.update({r["address"]:(r.get("handle") or r["address"][:6]) for r in rows})
        if not TRACKED:await asyncio.sleep(600);continue
        try:
            async with websockets.connect(CFG()["rpc_ws"],ping_interval=20,ping_timeout=20,max_size=2**22) as ws:
                ids={}
                for i,w in enumerate(TRACKED):ids[i+1]=w;await ws.send(json.dumps({"jsonrpc":"2.0","id":i+1,"method":"logsSubscribe","params":[{"mentions":[w]},{"commitment":"confirmed"}]}))
                subs={};sc.say("INFO",f"watching {len(TRACKED)} top wallets on-chain (fomo-api.com 7d PnL top 10)")
                end=time.time()+1800
                while time.time()<end:
                    raw=await asyncio.wait_for(ws.recv(),900);d=json.loads(raw)
                    if "id" in d and isinstance(d.get("result"),int):subs[d["result"]]=ids.get(d["id"]);continue
                    p=d.get("params") or {};w=subs.get(p.get("subscription"))
                    if w and not p["result"]["value"].get("err"):asyncio.create_task(decode_wallet(w,p["result"]["value"]["signature"]))
        except Exception as e:
            sc.say("INFO",f"wallet stream reconnect ({type(e).__name__})");await asyncio.sleep(10)
async def decode_wallet(w,sig):
    tx=await rpc("getTransaction",[sig,{"encoding":"jsonParsed","maxSupportedTransactionVersion":0,"commitment":"confirmed"}],10)
    if not isinstance(tx,dict):return
    meta=tx.get("meta") or {};chg=collections.defaultdict(float)
    for side,sgn in (("preTokenBalances",-1),("postTokenBalances",1)):
        for bl in meta.get(side) or []:
            if bl.get("owner")==w:chg[bl["mint"]]+=sgn*float(bl["uiTokenAmount"]["uiAmount"] or 0)
    who=TRACKED.get(w,w[:6])
    for mint,dq in chg.items():
        c=COINS.get(mint)
        if not c or abs(dq)<1e-9:continue
        if dq>0:c.flag_copy=who;A["SCOUT"].live("★",f"top trader {who} bought ${c.sym}");A["SCOUT"].say("INFO",f"top trader {who} bought ${c.sym}",mint,c.sym);ANALYST(mint,time.time())
# ---------------- bots ----------------
class Bot:
    def __init__(s,row):
        s.id=row["id"];s.name=row["name"];s.gen=row["gen"];s.g=json.loads(row["genome"]);s.style=row["style"];s.cash=row["cash"];s.start=row["start_cash"]
        s.parents=row["parents"];s.status=row["status"];s.epoch_start=row["epoch_start"] or time.time();s.pos={}   # mint -> pos dict
    def equity(s):return s.cash+sum(p["tok"]*strat.mid(COINS[m])*SOL[0] for m,p in s.pos.items() if m in COINS)+sum(p.get("stale",0) for m,p in s.pos.items() if m not in COINS)
    def closed(s,since=None):
        q="select cost,proceeds from bot_pos where bot=? and status='closed'"+(" and opened_ts>=?" if since else "")
        return db.execute(q,(s.id,since) if since else (s.id,)).fetchall()
BOTS={}
def holders(mint):return any(mint in b.pos for b in BOTS.values())
NAMES=["Viper","Kite","Onyx","Nova","Rook","Echo","Flux","Jinx","Pike","Volt","Lynx","Bolt","Hex","Saber","Mako","Zed","Orca","Rune","Wisp","Talon"]
def new_bot(g,gen,parents,cash,note=""):
    used={r[0] for r in db.execute("select name from bots")}
    base=[n for n in NAMES if not any(u.startswith(n) for u in used)] or NAMES
    name=f"{random.choice(base)}-{gen}"
    cur=db.execute("insert into bots(name,gen,parents,style,genome,born,status,cash,start_cash,epoch_start,note) values(?,?,?,?,?,?,?,?,?,?,?)",(name,gen,parents,g["style"],json.dumps(g),now(),"alive",cash,cash,time.time(),note));db.commit()
    row=db.execute("select * from bots where id=?",(cur.lastrowid,)).fetchone();BOTS[row["id"]]=Bot(row);return BOTS[row["id"]]
def load_bots():
    for row in db.execute("select * from bots where status='alive'"):
        b=Bot(row);BOTS[b.id]=b
        for p in db.execute("select * from bot_pos where bot=? and status='open'",(b.id,)):
            b.pos[p["mint"]]=dict(id=p["id"],entry_mcap=p["entry_mcap"],peak_mcap=p["peak_mcap"],opened=p["opened_ts"],tp_done=bool(p["tp_done"]),tok=p["tok"],cost=p["cost"],got=p["proceeds"],sym=p["sym"])
            if p["mint"] not in COINS and p["curve"]:   # resume: re-poll its curve after restart
                c=strat.Coin(p["mint"],p["sym"],p["opened_ts"]-60,None,0,p["entry_mcap"]);COINS[p["mint"]]=c;CURVE[p["mint"]]=p["curve"];META[p["mint"]]=dict(sym=p["sym"],stage="held",series=[],last_reason=collections.Counter(),deep="ok")
    if not BOTS:seed()
def seed():
    """Generation 0: train-set winners from the replay backtest (bt_result.json) + live-only styles."""
    c=CFG();desk=c["start_equity"];r=json.load(open(f"{D}/bt_result.json")) if os.path.exists(f"{D}/bt_result.json") else {"bots":[]}
    gs=[(x["genome"],f"backtest train n={x['train']['n']} mean {x['train']['mean']:+.1%}; holdout n={x['holdout']['n']} mean {x['holdout']['mean']:+.1%}") for x in r["bots"]][:8]
    rng=random.Random()
    for st in ("copy","migration"):
        g=strat.rand_genome("momentum",rng);g.update(style=st,stop=-0.15,tp1=0.4,tp1_frac=0.5,trail=0.15,fade_s=120,fade_r=0.0,max_hold=900,size_pct=0.08,min_age=5,max_mcap=300 if st=="copy" else 380,fresh_s=15,max_early=25,social=0)
        gs.append((g,"live-only style (no tape data to backtest it)"))
    while len(gs)<10:gs.append((strat.rand_genome(None,rng),"random explorer"))
    for g,note in gs[:10]:new_bot(g,0,"seed",desk/10,note)
    kvset("desk_start",desk);kvset("desk_t0",time.time());kvset("epoch",0);kvset("epoch_t0",time.time())
# ---------------- ANALYST ----------------
def ANALYST(mint,ts):
    c=COINS.get(mint);m=META.get(mint)
    if not c or m.get("killed") or m.get("deep")=="kill":return
    an=A["ANALYST"]
    for b in BOTS.values():
        if mint in b.pos or EXEC.pending_for(b.id,mint) or (b.id,mint) in DONE:continue
        why=strat.wants_entry(b.g,c,ts)
        if why:
            m["last_reason"][why]+=1;STATS["evals"]+=1
            if why not in("too early","too old") and random.random()<0.25:an.live("✕",f"${c.sym} {why} · {b.name}")
            continue
        STATS["signals"]+=1;an.total=STATS["signals"];hop("ANALYST","EXECUTOR")
        an.live("◆",f"{b.name} wants ${c.sym} · {b.style} · {c.mcap:.0f} SOL mc · {c.up}↑/{c.down}↓")
        asyncio.create_task(approve(b,mint))
DONE=set()   # (bot,mint) one round-trip per bot per coin
async def approve(b,mint):
    c=COINS.get(mint);m=META[mint]
    if m.get("deep") in (None,"pending"):
        if m.get("deep") is None:r=await deep_check(mint)
        else:
            for _ in range(20):
                await asyncio.sleep(0.3)
                if m.get("deep")!="pending":break
    if m.get("deep")!="ok" or (b.id,mint) in DONE or mint in b.pos:return
    v=risk_veto(b,c)
    if v:A["RISK"].live("⛔",f"{b.name} ${c.sym} veto: {v}");return
    DONE.add((b.id,mint));EXEC.order(b,mint,"buy",1.0,f"{b.style}: {c.up}↑/{c.down}↓, +{c.inflow:.1f} SOL in, {c.mcap:.0f} SOL mc, age {time.time()-c.born:.0f}s")
def risk_veto(b,c):
    if os.path.exists(f"{DATA}/KILL") or CFG().get("kill_switch"):return "kill switch"
    if len(b.pos)>=CFG()["max_open_per_bot"]:return f"max {CFG()['max_open_per_bot']} open"
    eq=b.equity()
    if eq<b.start*CFG()["bot_floor_pct"]:return "bot below capital floor"
    if b.cash<1:return "no cash"
    return None
# ---------------- EXECUTOR: paper fills with >=1s latency on the real curve ----------------
class Executor:
    def __init__(s):s.q=collections.defaultdict(list)
    def pending_for(s,bid,mint):return any(o["bot"]==bid for o in s.q.get(mint,[]))
    def order(s,b,mint,side,frac,why):
        s.q[mint].append(dict(bot=b.id,side=side,frac=frac,why=why,t=time.time()))
        A["EXECUTOR"].live("…",f"{b.name} {side} ${COINS[mint].sym} queued, fills next tick")
    def fill_pending(s,mint,ts):
        for o in [o for o in s.q.get(mint,[]) if ts-o["t"]>=1.0]:
            s.q[mint].remove(o);s.fill(o,mint,ts)
    async def sweeper(s):
        """Orders on coins with no new curve change still fill at the current state after 1.5s (nothing traded = price unchanged)."""
        while True:
            await asyncio.sleep(0.5);ts=time.time()
            for mint in list(s.q):
                for o in [o for o in s.q[mint] if ts-o["t"]>=1.5]:s.q[mint].remove(o);s.fill(o,mint,ts)
    def fill(s,o,mint,ts):
        b=BOTS.get(o["bot"]);c=COINS.get(mint);ex=A["EXECUTOR"]
        if not b or not c or SOL[0]<=0:return
        if o["side"]=="buy":
            usd_sz=min(b.cash,max(b.equity()*b.g["size_pct"],0.05*SOL[0]));sol=usd_sz/SOL[0]
            if sol<0.049:return
            tok=strat.buy_fill(c,sol)
            if tok<=0:return
            b.cash-=usd_sz
            cur=db.execute("insert into bot_pos(bot,mint,sym,curve,opened,opened_ts,entry_mcap,peak_mcap,tok,tok0,cost,status,reason,sol_px) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(b.id,mint,c.sym,CURVE.get(mint),now(),ts,c.mcap,c.mcap,tok,tok,usd_sz,"open",o["why"],SOL[0]))
            db.execute("update bots set cash=? where id=?",(b.cash,b.id))
            b.pos[mint]=dict(id=cur.lastrowid,entry_mcap=c.mcap,peak_mcap=c.mcap,opened=ts,tp_done=False,tok=tok,cost=usd_sz,got=0.0,sym=c.sym)
            STATS["buys"]+=1;ex.total=STATS["buys"];hop("EXECUTOR","RISK")
            ex.live("↗",f"{b.name} buy ${c.sym} {sol:.2f} SOL @ {c.mcap:.0f} SOL mc")
            ex.say("BUY",f"{b.name} BUY ${c.sym} ${usd_sz:.2f} ({sol:.2f} SOL) at {c.mcap:.0f} SOL mc · {o['why']}",mint,c.sym,head=True)
        else:
            p=b.pos.get(mint)
            if not p:return
            q=p["tok"]*o["frac"];sol=strat.sell_fill(c,q);got=sol*SOL[0];p["tok"]-=q;p["got"]+=got;b.cash+=got
            full=o["frac"]>=0.999 or p["tok"]*strat.mid(c)*SOL[0]<0.05
            if full:
                b.cash+=p["tok"]*0;pnl=p["got"]-p["cost"];del b.pos[mint]
                db.execute("update bot_pos set tok=0,proceeds=?,status='closed',closed=?,exit_reason=coalesce(exit_reason||'; ','')||? where id=?",(p["got"],now(),o["why"],p["id"]))
                STATS["closed"]+=1;STATS["wins" if pnl>0 else "losses"]+=1
                if "stop" in o["why"]:STATS["stops"]+=1;A["RISK"].total=STATS["stops"]
                ex.live("✓" if pnl>0 else "✕",f"{b.name} sold ${c.sym} {'+' if pnl>=0 else '−'}${abs(pnl):.2f} ({o['why']})")
                ex.say("SELL",f"{b.name} SOLD ${c.sym} {'+' if pnl>=0 else '-'}${abs(pnl):.2f} ({pnl/p['cost']:+.0%}) · {o['why']}",mint,c.sym,round(pnl,2),head=True)
            else:
                p["tp_done"]=True
                db.execute("update bot_pos set tok=?,proceeds=?,tp_done=1,exit_reason=coalesce(exit_reason||'; ','')||? where id=?",(p["tok"],p["got"],o["why"],p["id"]))
                ex.live("✓",f"{b.name} took {o['frac']:.0%} of ${c.sym} ({o['why']})");ex.say("SELL",f"{b.name} sold {o['frac']:.0%} ${c.sym} +${got:.2f} · {o['why']}",mint,c.sym)
            db.execute("update bots set cash=? where id=?",(b.cash,b.id))
EXEC=Executor()
# ---------------- RISK: exits, watchdog, evolution ----------------
def RISKMGR(mint,ts):
    c=COINS.get(mint)
    if not c:return
    for b in BOTS.values():
        p=b.pos.get(mint)
        if not p or EXEC.pending_for(b.id,mint):continue
        r=strat.exit_reason(b.g,c,p,ts)
        if r:
            A["RISK"].live("⚑",f"{b.name} exit ${c.sym}: {r[0]}");EXEC.order(b,mint,"sell",r[1],r[0])
async def risk_clock():
    """Time-based exits on quiet coins + desk mark-to-market every second."""
    last_eq=0;last_kv=0
    while True:
        await asyncio.sleep(1);ts=time.time();WD["loop"]=ts
        for mint in [m for m in COINS if holders(m)]:RISKMGR(mint,ts)
        eq=desk_equity();push("eq",[round(ts,2),round(eq,4)])
        op=[(b,m,p) for b in BOTS.values() for m,p in b.pos.items() if m in COINS]
        if op:
            b,m,p=op[int(ts)%len(op)];c=COINS[m];A["RISK"].live("◔",f"{b.name} ${c.sym} {c.mcap/p['entry_mcap']-1:+.0%} · stop {b.g['stop']:.0%}")
        elif ts-A["RISK"].last>5:A["RISK"].live("◔",f"0 open · {sum(1 for _ in BOTS)} bots armed · stops by rules")
        if ts-A["EXECUTOR"].last>8:
            r=db.execute("select b.name,p.sym,p.proceeds-p.cost from bot_pos p join bots b on b.id=p.bot where p.status='closed' order by p.id desc limit 1").fetchone()
            A["EXECUTOR"].live("·",f"last {'+' if r[2]>=0 else '−'}${abs(r[2]):.2f} ${r[1]} ({r[0]}) · waiting" if r else "waiting · nothing passes yet")
        if ts-last_eq>=15:db.execute("insert into equity values(?,?)",(now(),eq));db.commit();last_eq=ts
        if ts-last_kv>=30:kvset("kills",dict(KILLS));kvset("stats",dict(STATS));last_kv=ts;retire_old()
def desk_equity():return sum(b.equity() for b in BOTS.values())+kv_cache.get("dead_cash",0)
kv_cache={}
def retire_old():
    """Drop quiet coins from the poll set; record their tick series for future backtests."""
    ts=time.time();cfg=CFG()
    for mint,c in list(COINS.items()):
        if holders(mint) or EXEC.q.get(mint):continue
        m=META[mint];quiet=ts-c.last_ts;age=ts-c.born
        if m.get("killed") and age>30 or (age>cfg["max_watch_s"] and quiet>90) or age>3600:
            if not m.get("killed"):
                lr=m["last_reason"].most_common(1);why="never pumped" if c.peak<35 else (f"no bot entry: {lr[0][0]}" if lr else "no bot entry")
                if any((b,mint) in DONE for b in BOTS):why=None
                if why:kill(why);STATS["passed"]+=1
            if len(m.get("series",[]))>3:
                db.execute("insert or replace into ticks values(?,?,?,?,?,?,?)",(mint,c.sym,c.born,c.creator,c.dev_pct,int(bool(c.social_ok)),json.dumps(m["series"])))
            COINS.pop(mint,None);CURVE.pop(mint,None);META.pop(mint,None)
    if STATS["ticks_prune"]%20==0:db.execute("delete from ticks where born<?",(ts-7*86400,))
    STATS["ticks_prune"]+=1;db.commit()
def score(b):
    rs=[(r["proceeds"]-r["cost"])/r["cost"] for r in b.closed(b.epoch_start) if r["cost"]>0];n=len(rs)
    if n<CFG()["min_trades_rank"]:return None,n
    m=sum(rs)/n;sd=math.sqrt(sum((x-m)**2 for x in rs)/max(n-1,1))+1e-6;return m/sd*math.sqrt(n),n
async def evolution():
    rk=A["RISK"]
    while True:
        await asyncio.sleep(30)
        t0=kv("epoch_t0",time.time());per=CFG()["evolve_hours"]*3600
        if time.time()-t0<per:continue
        ep=kv("epoch",0)+1;rows=[]
        for b in BOTS.values():
            s,n=score(b);rows.append((b,s,n,b.equity()-b.start))
        ranked=sorted([r for r in rows if r[1] is not None],key=lambda r:-r[1])
        idle=[r for r in rows if r[1] is None and r[2]==0]
        k=CFG()["replace_per_epoch"]
        losers=[r[0] for r in ranked[::-1] if r[1]<0 or r[3]<0][:k]
        losers+= [r[0] for r in idle if r[0] not in losers][:max(0,k-len(losers))]
        leaders=[r[0] for r in ranked if r[0] not in losers][:3] or [r[0] for r in rows if r[0] not in losers][:3]
        pool=0;added=[]
        for b in losers:   # liquidate at market
            for mint in list(b.pos):
                c=COINS.get(mint)
                if c:EXEC.fill(dict(bot=b.id,side="sell",frac=1.0,why="bot replaced",t=0),mint,time.time())
            pool+=b.equity();db.execute("update bots set status='replaced',died=?,cash=0 where id=?",(now(),b.id));BOTS.pop(b.id)
        rng=random.Random()
        for i,_ in enumerate(losers):
            if len(leaders)>=2 and i==len(losers)-1:a,bb=rng.sample(leaders,2);g=strat.mutate(strat.crossover(a.g,bb.g,rng),rng,0.2,0.1);par=f"{a.name}×{bb.name}"
            else:a=leaders[i%len(leaders)];g=strat.mutate(a.g,rng);par=a.name
            added.append(new_bot(g,max(x.gen for x in leaders)+1 if leaders else ep,par,0,f"epoch {ep} child"))
        if added or pool:
            base=pool*0.5/max(len(added),1)
            for nb in added:nb.cash=nb.start=base;db.execute("update bots set cash=?,start_cash=? where id=?",(base,base,nb.id))
            rest=pool-base*len(added);ws=[3,2,1][:len(leaders)]
            for w,l in zip(ws,leaders):l.cash+=rest*w/sum(ws);db.execute("update bots set cash=? where id=?",(l.cash,l.id))
        for b in BOTS.values():b.start=b.equity();b.epoch_start=time.time();db.execute("update bots set start_cash=?,epoch_start=? where id=?",(b.start,b.epoch_start,b.id))
        db.execute("insert into evolutions(ts,epoch,ranking,removed,added) values(?,?,?,?,?)",(now(),ep,json.dumps([[r[0].name,r[1],r[2],round(r[3],2)] for r in rows]),json.dumps([b.name for b in losers]),json.dumps([b.name for b in added])))
        kvset("epoch",ep);kvset("epoch_t0",time.time());db.commit()
        rk.say("EVOLVE",f"Epoch {ep}: replaced {', '.join(b.name for b in losers) or 'nobody'} → {', '.join(b.name+' ('+b.parents+')' for b in added) or '-'}; capital to {', '.join(l.name for l in leaders)}",head=True)
async def watchdog():
    rk=A["RISK"]
    while True:
        await asyncio.sleep(10);ts=time.time()
        if ts-WD["create"]>120 and WS[0]:
            rk.say("WARN","watchdog: no new launches for 120s - forcing PumpPortal reconnect");WD["create"]=ts
            try:await WS[0].close()
            except Exception:pass
        if ts-WD["poll"]>180 and ts-WD.get("pollwarn",0)>300:
            WD["pollwarn"]=ts;rk.say("WARN","watchdog: RPC curve polling throttled for 3+ min (public RPC limit) - prices stale, bots will not enter")
        if ts-WD["loop"]>60 or ts-WD["create"]>600:
            rk.say("WARN","watchdog: feed stalled - restarting the engine process");db.commit();kvset("stats",dict(STATS));os._exit(3)
async def sol_price():
    while True:
        d=await jget("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd")
        v=(d or {}).get("solana",{}).get("usd")
        if not v:
            d=await jget("https://api.dexscreener.com/latest/dex/pairs/solana/58oqchx4ywmvkdwllzzbi4chocc2fqcuwbkwmihlyqo2");v=float(((d or {}).get("pair") or {}).get("priceUsd") or 0) or None
        if v:SOL[0]=float(v)
        await asyncio.sleep(60 if SOL[0] else 5)
async def flusher():
    """Batch funnel lines + relay hops to the UI ~6x/s (only real events)."""
    while True:
        await asyncio.sleep(0.16)
        ch=[a.view() for a in A.values() if a.dirty]
        for a in A.values():a.dirty=False
        if ch or HOPS:push("tick",dict(agents=ch,hops=[[a,b,n] for (a,b),n in HOPS.items()]));HOPS.clear()
# ---------------- views ----------------
def bots_view():
    out=[]
    for b in BOTS.values():
        cl=b.closed();w=sum(1 for r in cl if r["proceeds"]>r["cost"]);s,n=score(b)
        out.append(dict(id=b.id,name=b.name,gen=b.gen,style=b.style,parents=b.parents,equity=round(b.equity(),2),start=round(b.start,2),pnl=round(b.equity()-b.start,2),trades=len(cl),wins=w,open=len(b.pos),score=None if s is None else round(s,2),status="alive"))
    out.sort(key=lambda x:-x["pnl"])
    for r in db.execute("select name,gen,style,parents,died from bots where status='replaced' order by id desc limit 6"):out.append(dict(name=r["name"],gen=r["gen"],style=r["style"],parents=r["parents"],status="replaced",died=r["died"]))
    return out
def tape():
    xs=[c for m,c in COINS.items() if not META[m].get("killed") and len(c.hist)>2 and c.up>=3]
    xs.sort(key=lambda c:-(c.last_ts));out=[]
    for c in xs[:24]:out.append(dict(sym=c.sym,px=strat.mid(c)*SOL[0],ch=round((c.mcap/c.hist[0][1]-1)*100,1)))
    return out
def stats():
    c=CFG();eq=desk_equity();st=kv("desk_start",c["start_equity"])
    return dict(start=st,equity=round(eq,4),pnl=round(eq-st,2),wins=STATS["wins"],losses=STATS["losses"],open=sum(len(b.pos) for b in BOTS.values()),
        seen=STATS["seen"],killed=STATS["killed"],signals=STATS["signals"],buys=STATS["buys"],closed=STATS["closed"],stops=STATS["stops"],updates=STATS["updates"],
        watching=len(COINS),sol=SOL[0],uptime=int(time.time()-START),t0=kv("desk_t0",START),epoch=kv("epoch",0),next_evolve=kv("epoch_t0",time.time())+c["evolve_hours"]*3600,
        feeds=dict(pumpportal=bool(WS[0]),rpc_poll=time.time()-WD["poll"]<10),profile=c.get("profile"),sub=c["subscription"])
def snapshot():
    return dict(stats=stats(),agents=[a.view() for a in A.values()],server_now=time.time(),kills=KILLS.most_common(12),bots=bots_view(),tape=tape(),head=HEAD,
        equity=[list(r) for r in db.execute("select ts,equity from (select ts,equity from equity order by ts desc limit 1500) order by ts")],
        log=[dict(r) for r in db.execute("select ts,agent,tag,msg from events order by id desc limit 60")])
# ---------------- web ----------------
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
async def healthz(req):
    ts=time.time();ok=ts-WD["loop"]<30 and ts-WD["poll"]<120
    return web.Response(text="ok" if ok else "stalled",status=200 if ok else 503)
async def api_state(req):return web.json_response(snapshot(),headers={"Cache-Control":"no-store"})
async def api_backtest(req):
    p=f"{D}/bt_result.json";return web.json_response(json.load(open(p)) if os.path.exists(p) else {},headers={"Cache-Control":"no-store"})
async def api_evolutions(req):return web.json_response([dict(r) for r in db.execute("select * from evolutions order by id desc limit 50")])
async def api_stream(req):
    r=web.StreamResponse(headers={"Content-Type":"text/event-stream","Cache-Control":"no-store","X-Accel-Buffering":"no"});await r.prepare(req)
    q=asyncio.Queue(4000);SUBS.add(q)
    try:
        await r.write(f"event: hello\ndata: {json.dumps(snapshot())}\n\n".encode());last=time.time()
        while True:
            try:m=await asyncio.wait_for(q.get(),1)
            except asyncio.TimeoutError:m=None
            if m:await r.write(m.encode())
            if time.time()-last>=2:await r.write(f"event: stats\ndata: {json.dumps(dict(stats=stats(),server_now=time.time(),kills=KILLS.most_common(12),bots=bots_view(),tape=tape()))}\n\n".encode());last=time.time()
    except (ConnectionResetError,asyncio.CancelledError,Exception):pass
    finally:SUBS.discard(q)
    return r
async def main():
    HTTP[0]=aiohttp.ClientSession(headers={"User-Agent":"Mozilla/5.0 (sage paper terminal)"})
    load_bots()
    # close legacy v4 single-bot positions (no live state for them anymore)
    db.execute("update positions set status='closed',closed=?,exit_reason='v5 migration' where status='open'",(now(),));db.commit()
    app=web.Application(middlewares=[auth])
    app.router.add_get("/healthz",healthz)
    app.router.add_get("/login",static("login.html"));app.router.add_post("/login",login_post);app.router.add_get("/logout",logout)
    for n in ("manifest.json","icon-192.png","icon-512.png","apple-touch-icon.png","favicon.ico"):app.router.add_get("/"+n,static(n))
    app.router.add_get("/sw.js",static("sw.js","application/javascript"))
    app.router.add_get("/",static("index.html"));app.router.add_get("/api/state",api_state);app.router.add_get("/api/stream",api_stream)
    app.router.add_get("/api/backtest",api_backtest);app.router.add_get("/api/evolutions",api_evolutions)
    run=web.AppRunner(app);await run.setup();await web.TCPSite(run,"0.0.0.0",int(os.environ.get("PORT","8787"))).start()
    A["RISK"].say("INFO",f"engine v5 up: {len(BOTS)} paper bots, epoch {kv('epoch',0)}",head=True)
    await asyncio.gather(hub(),poller(),wallets(),dev_watch(),risk_clock(),evolution(),watchdog(),sol_price(),flusher(),EXEC.sweeper())
if __name__=="__main__":asyncio.run(main())

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
DBF=f"{DATA}/sage_v10.db";CFG=lambda:json.load(open(f"{D}/config.json"))
PW=os.environ.get("DASH_PASSWORD","");START=time.time()
def now():return dt.datetime.now().isoformat(timespec="milliseconds")
# ---------------- storage ----------------
db=sqlite3.connect(DBF,check_same_thread=False);db.row_factory=sqlite3.Row
try:db.execute("alter table bots add column strikes int default 0");db.commit()
except Exception:pass
db.executescript("""create table if not exists events(id integer primary key,ts text,agent text,tag text,msg text,mint text,sym text,pnl real);
create table if not exists positions(id integer primary key,mint text,sym text,opened text,closed text,entry real,qty real,cost real,peak real,last real,remaining real,proceeds real default 0,fees real default 0,status text,reason text,exit_reason text,tp int default 0,creator text);
create table if not exists equity(ts text,equity real);
create table if not exists signals(id integer primary key,ts text,mint text,sym text,stage text,status text,reason text);
create table if not exists bots(id integer primary key,name text,gen int,parents text,style text,genome text,born text,died text,status text,cash real,start_cash real,epoch_start real,note text);
create table if not exists bot_pos(id integer primary key,bot int,mint text,sym text,curve text,opened text,opened_ts real,closed text,entry_mcap real,peak_mcap real,tok real,tok0 real,cost real,proceeds real default 0,tp_done int default 0,status text,reason text,exit_reason text,sol_px real);
create table if not exists evolutions(id integer primary key,ts text,epoch int,ranking text,removed text,added text);
create table if not exists kv(k text primary key,v text);
create table if not exists equity2(ts real,total real,crypto real,meme real,bench real);
create table if not exists ticks(mint text primary key,sym text,born real,creator text,dev_pct real,social int,series text);""");db.commit()
for _q in ("alter table bots add column strikes int default 0","alter table bot_pos add column wallets text","alter table bots add column lessons text"):
    try:db.execute(_q);db.commit()
    except Exception:pass
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
A={n:Agent(n,c,r) for n,c,r in [("EXECUTIVE","#C9A35B","allocates capital"),("SCREENER","#5B8CFF","1H/4H trend screen"),("ENTRY PLANNER","#7DD3FC","plans entry/stop/target"),("RISK OFFICER","#F0B35B","sizing & vetoes"),("EXIT CLERK","#A78BFA","stops, targets, trails"),("AUDITOR","#94A3B8","fills, fees, scorecard"),("SCOUT","#3399FF","finds every pump.fun launch"),("FILTER","#FF4444","kills rugs & traps"),("ANALYST","#A020F0","scores coins for each bot"),("EXECUTOR","#00FF41","paper fills on the curve"),("RISK","#FF3399","stops, limits, evolution")]}
HEAD=["engine starting","SAGE v5"];HOPS=collections.Counter()
def hop(a,b):HOPS[(a,b)]+=1
KILLS=collections.Counter(kv("kills",{}) or {})
STATS=collections.Counter(kv("stats",{}) or {})
import re
def kill(reason):
    r=re.sub(r"dev bought \d+%","dev bought >10%",reason);r=re.sub(r"snipers \d+% in 3s","snipers >25% in 3s",r);r=re.sub(r"(no bot entry: )?(mcap|inflow) [\d.]+ [<>] [\d.]+",lambda m:(m.group(1) or "")+m.group(2)+" outside bot range",r);r=re.sub(r"\d+ buys < \d+","too few buys",r);r=re.sub(r"snipers \d+%","snipers too heavy",r)
    KILLS[r]+=1
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
async def rpc(method,params,timeout=8,reverse=False):
    """Free keyless RPC pool with failover (PublicNode first, Solana public RPC second). Wallet decoding uses the reverse order so it never starves curve polling."""
    pool=CFG()["rpc_http_pool"][::-1] if reverse else CFG()["rpc_http_pool"]
    if reverse:
        for u in pool:
            try:
                async with HTTP[0].post(u,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    if r.status==200:
                        j=await r.json(content_type=None)
                        if "result" in j:return j["result"]
            except Exception:pass
            break   # only the secondary endpoint for low-priority calls
        return "429"
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
    dev_pct=100*(m.get("initialBuy") or 0)/1e9;sym=(m.get("symbol") or "?").lstrip("$")[:12] or "?"
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
HOURLY=collections.Counter();CHECK=collections.OrderedDict()   # mint -> live checklist card for the CHECKING column
def chk(mint,step=None,status=None,verdict=None,signal=None):
    m=META.get(mint);c=COINS.get(mint)
    if not m:return
    e=CHECK.get(mint)
    if not e:
        e=CHECK[mint]=dict(mint=mint,sym=m["sym"],ts=time.time(),steps={},signals=[],verdict="checking")
        while len(CHECK)>40:CHECK.popitem(last=False)
    e["ts"]=time.time()
    if step:e["steps"][step]=status
    if verdict:e["verdict"]=verdict
    if signal and signal not in e["signals"]:e["signals"].append(signal)
    if c:e["mcap"]=round(c.mcap,1)
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
    m["deep"]="pending";f=A["FILTER"];f.live("⌕",f"RugCheck + dev wallet ${m['sym']}");chk(mint,"dev wallet","…");chk(mint,"RugCheck","…")
    if c.dev_pct>0.05 and c.creator:
        b=await dev_balance(c.creator,mint)
        if b is not None and b<1e7*c.dev_pct*0.5:c.dev_sold=True;mark_rugger(c)
    chk(mint,"dev wallet","sold" if c.dev_sold else f"holds · bought {c.dev_pct:.1f}%")
    r=await jget(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report",6);m["rug_ok"]=bool(r)
    sym_=((r or {}).get("tokenMeta") or {}).get("symbol")
    if sym_ and m["sym"]==mint[:4]:
        m["sym"]=c.sym=sym_.lstrip("$")[:12]
        if mint in CHECK:CHECK[mint]["sym"]=m["sym"]
    if r and r.get("mintAuthority"):c.hard_kill="mint authority live"
    elif r and r.get("freezeAuthority"):c.hard_kill="freeze authority live"
    elif r:
        top=sum(float(h.get("pct") or 0) for h in (r.get("topHolders") or [])[:11] if h.get("address")!=CURVE.get(mint) and h.get("owner")!=CURVE.get(mint))
        m["top10"]=top
        if top>CFG().get("max_top10_pct",35):c.hard_kill=f"top10 holders {top:.0f}%"
        chk(mint,"top holders",f"{top:.0f}%")
    chk(mint,"RugCheck","authorities revoked" if r and not c.hard_kill else ("not indexed" if not r else c.hard_kill))
    chk(mint,"snipers",f"{c.early_pct:.0f}%");chk(mint,"socials",m.get("social") or "unknown")
    why=strat.hard_kill(c,time.time())
    if why:retire_kill(mint,why);m["deep"]="kill";chk(mint,verdict="killed: "+why);f.say("KILL",f"${m['sym']} {why}",mint,m["sym"]);return "kill"
    m["deep"]="ok";chk(mint,verdict="passed checks");f.live("✓",f"${m['sym']} clean: dev {c.dev_pct:.1f}%, snipers {c.early_pct:.0f}%");hop("FILTER","ANALYST");return "ok"
async def dev_watch():
    """Every 10s: dev balance for coins any bot holds (dev sell = forced exit)."""
    while True:
        await asyncio.sleep(10)
        for mint in [m for m in COINS if holders(m)]:
            c=COINS[mint]
            if c.dev_pct>0.05 and not c.dev_sold:
                b=await dev_balance(c.creator,mint)
                if b is not None and b<1e7*c.dev_pct*0.5:c.dev_sold=True;mark_rugger(c);A["RISK"].say("WARN",f"${c.sym} dev wallet sold - forcing exits",mint,c.sym,head=True)
# ---------------- TRACKER (part of SCOUT): top wallets via logsSubscribe ----------------
TRACKED={};BENCHED=set(kv("benched",[]) or []);WSTREAK=collections.Counter();WSWAP=[False]
def wallet_result(w,win):
    """Copied wallets: 2 straight losing copies -> swap the wallet for the next one on the leaderboard."""
    who=TRACKED.get(w,w[:6])
    if not w:return
    WSTREAK[w]=0 if win else WSTREAK[w]+1
    if WSTREAK[w]>=2:
        BENCHED.add(w);kvset("benched",list(BENCHED));WSWAP[0]=True
        A["RISK"].say("EVOLVE",f"copied wallet {who} benched after 2 straight losing copies - swapping in the next leaderboard wallet",head=True)
SMART={}   # address -> dict(handle, ret, trades, pnl, window, rank)
async def smart_list():
    """fomo-api.com realized-PnL leaderboards (24h/7d/30d), keyless pages of 10. Smart-wallet filter: coverage>=80%, return>=5%, 10..600 fills, not benched."""
    out={}
    for win in ("24h","7d","30d"):
        cur=None
        for _ in range(4):
            d=await jget(f"https://api.fomo-api.com/v1/leaderboard?window={win}&board=solana&limit=10"+(f"&cursor={cur}" if cur else ""),15)
            for r in (d or {}).get("data") or []:
                a=r["address"]
                if a in BENCHED or a in out:continue
                if (r.get("coveragePct") or 0)<80 or (r.get("returnPct") or 0)<5 or not 10<=(r.get("trades") or 0)<=600:continue
                out[a]=dict(handle=r.get("handle") or r.get("displayName") or a[:6],ret=r.get("returnPct") or 0,trades=r.get("trades") or 0,pnl=r.get("realizedPnlUsd") or 0,window=win,rank=r.get("rank"))
            cur=((d or {}).get("pageInfo") or {}).get("nextCursor")
            await asyncio.sleep(1.5)
            if not cur:break
    out=dict(sorted(out.items(),key=lambda kv_:-kv_[1]["pnl"])[:CFG().get("max_smart_wallets",60)])
    try:   # wallets that were profitable on real pump.fun bonding-curve tapes (harvested on the box, see copybt.py)
        for a,v in json.load(open(f"{D}/smart_wallets.json")).items():
            if a not in BENCHED:out.setdefault(a,v)
    except Exception:pass
    return out
async def wallets():
    sc=A["SCOUT"];last=0
    while True:
        if time.time()-last>1800 or not SMART or WSWAP[0]:
            new=await smart_list()
            if new:SMART.clear();SMART.update(new);TRACKED.clear();TRACKED.update({a:v["handle"] for a,v in new.items()})
            last=time.time()
        if not SMART:await asyncio.sleep(300);continue
        ws_=list(SMART);chunks=[ws_[i:i+35] for i in range(0,len(ws_),35)]
        sc.say("INFO",f"copy desk: watching {len(SMART)} smart wallets on-chain over {len(chunks)} streams (fomo-api.com boards + wallets profitable on real pump.fun tapes)")
        WSWAP[0]=False
        await asyncio.gather(*[wallet_stream(ch,i) for i,ch in enumerate(chunks)])
async def wallet_stream(chunk,k):
    end=time.time()+1800;await asyncio.sleep(k*2)
    while time.time()<end and not WSWAP[0]:
        try:
            async with websockets.connect(CFG()["rpc_ws"],ping_interval=20,ping_timeout=20,max_size=2**22) as ws:
                ids={}
                for i,w in enumerate(chunk):ids[i+1]=w;await ws.send(json.dumps({"jsonrpc":"2.0","id":i+1,"method":"logsSubscribe","params":[{"mentions":[w]},{"commitment":"confirmed"}]}));await asyncio.sleep(0.05)
                subs={};STATS["w_streams"]+=1
                while time.time()<end and not WSWAP[0]:
                    try:raw=await asyncio.wait_for(ws.recv(),30)
                    except asyncio.TimeoutError:continue
                    d=json.loads(raw)
                    if "id" in d and isinstance(d.get("result"),int):subs[d["result"]]=ids.get(d["id"]);continue
                    p=d.get("params") or {};w=subs.get(p.get("subscription"))
                    v=((p.get("result") or {}).get("value") or {})
                    if w and v:STATS["w_notif"]+=1
                    if w and v and not v.get("err") and PUMP in " ".join(v.get("logs") or []):
                        STATS["w_pump"]+=1;asyncio.create_task(decode_wallet(w,v.get("signature")))
        except Exception as e:
            STATS["w_drops"]+=1;await asyncio.sleep(5+random.random()*10)
PUMP="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
def curve_pda(mint):
    from solders.pubkey import Pubkey
    return str(Pubkey.find_program_address([b"bonding-curve",bytes(Pubkey.from_string(mint))],Pubkey.from_string(PUMP))[0])
async def adopt(mint):
    """A smart wallet traded a pump.fun coin we are not watching: start polling its bonding curve."""
    j=await jget(f"https://frontend-api-v3.pump.fun/coins/{mint}",6) or {}
    if j.get("complete"):return None
    born=(j.get("created_timestamp") or time.time()*1000-600e3)/1000
    c=strat.Coin(mint,(j.get("symbol") or mint[:4])[:12],born,j.get("creator"),0.0,28.0)
    c.social_ok=bool(j.get("twitter") or j.get("website")) if j else None
    COINS[mint]=c;CURVE[mint]=j.get("bonding_curve") or curve_pda(mint)
    META[mint]=dict(sym=c.sym,uri=None,stage="copy",series=[],last_reason=collections.Counter(),social="X/site" if c.social_ok else "none")
    r=await rpc("getMultipleAccounts",[[CURVE[mint]],{"encoding":"base64","commitment":"processed"}])
    if isinstance(r,dict) and r.get("value") and r["value"][0]:
        on_curve(mint,r["value"][0]["data"][0],time.time())
        if not c.graduated:return c
    COINS.pop(mint,None);CURVE.pop(mint,None);META.pop(mint,None);return None   # not a live pump.fun curve (graduated or other DEX)
async def decode_wallet(w,sig):
    if DECSEM.locked():STATS["w_skipped"]+=1;return
    async with DECSEM:
        tx=await rpc("getTransaction",[sig,{"encoding":"jsonParsed","maxSupportedTransactionVersion":0,"commitment":"confirmed"}],10,reverse=True);await asyncio.sleep(0.5)
    if not isinstance(tx,dict):STATS["w_txfail"]+=1;return
    meta=tx.get("meta") or {};chg=collections.defaultdict(float)
    for side,sgn in (("preTokenBalances",-1),("postTokenBalances",1)):
        for bl in meta.get(side) or []:
            if bl.get("owner")==w:chg[bl["mint"]]+=sgn*float(bl["uiTokenAmount"]["uiAmount"] or 0)
    STATS["w_decoded"]+=1;WDBG.append((w[:6],{k[:6]:round(v) for k,v in chg.items()}))
    sw=SMART.get(w) or dict(handle=w[:6],ret=0,trades=0);who=sw["handle"];ts=time.time()
    for mint,dq in chg.items():
        if abs(dq)<1e-9 or mint.startswith("So111") or mint.startswith("EPjFW") or mint.startswith("Es9vM"):continue
        c=COINS.get(mint)
        if not c and dq>0:c=await adopt(mint)
        if not c:continue
        if dq>0:
            c.flag_copy=(c.flag_copy or [])+[(ts,w,sw["ret"],sw["trades"])];STATS["copy_signals"]+=1
            chk(mint,signal=who);chk(mint,"smart wallets",f"{len({x[1] for x in c.flag_copy})} bought")
            A["SCOUT"].live("★",f"smart wallet {who} bought ${c.sym} at {c.mcap:.0f} SOL mc");A["SCOUT"].say("INFO",f"smart wallet {who} (+{sw['ret']:.0f}% {sw.get('window','')}) bought ${c.sym} at {c.mcap:.0f} SOL mc",mint,c.sym)
            hop("SCOUT","ANALYST");ANALYST(mint,ts)
        else:
            SELLSIG[mint][w]=ts;A["SCOUT"].live("☆",f"smart wallet {who} sold ${c.sym}")
            if holders(mint):A["SCOUT"].say("INFO",f"smart wallet {who} sold ${c.sym}",mint,c.sym);RISKMGR(mint,ts)
DECSEM=asyncio.Semaphore(2);SELLSIG=collections.defaultdict(dict);WDBG=collections.deque(maxlen=20)
# ---------------- bots ----------------
class Bot:
    def __init__(s,row):
        s.id=row["id"];s.name=row["name"];s.gen=row["gen"];s.g=json.loads(row["genome"]);s.style=row["style"];s.cash=row["cash"];s.start=row["start_cash"]
        s.strikes=row["strikes"] or 0;s.parents=row["parents"];s.status=row["status"];s.epoch_start=row["epoch_start"] or time.time();s.pos={};s.lessons=row["lessons"] or "";s.day={}
        for r_ in db.execute("select substr(opened,1,10) d,count(*) n from bot_pos where bot=? group by d",(row["id"],)):s.day[r_[0]]=r_[1]   # mint -> pos dict
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
    while name in used:name=f"{random.choice(NAMES)}-{gen}{random.choice('abcdefghjk')}"
    cur=db.execute("insert into bots(name,gen,parents,style,genome,born,status,cash,start_cash,epoch_start,note) values(?,?,?,?,?,?,?,?,?,?,?)",(name,gen,parents,g["style"],json.dumps(g),now(),"alive",cash,cash,time.time(),note));db.commit()
    row=db.execute("select * from bots where id=?",(cur.lastrowid,)).fetchone();BOTS[row["id"]]=Bot(row);return BOTS[row["id"]]
def load_bots():
    for row in db.execute("select * from bots where status='alive'"):
        b=Bot(row);BOTS[b.id]=b
        k=0
        for r in db.execute("select proceeds,cost from bot_pos where bot=? and status='closed' order by id desc",(b.id,)):
            if r[0]>r[1]:break
            k+=1
        b.strikes=k
        for p in db.execute("select * from bot_pos where bot=? and status='open'",(b.id,)):
            b.pos[p["mint"]]=dict(id=p["id"],entry_mcap=p["entry_mcap"],peak_mcap=p["peak_mcap"],opened=p["opened_ts"],tp_done=bool(p["tp_done"]),tok=p["tok"],cost=p["cost"],got=p["proceeds"],sym=p["sym"],wallets=json.loads(p["wallets"] or "[]"))
            if p["mint"] not in COINS and p["curve"]:   # resume: re-poll its curve after restart
                c=strat.Coin(p["mint"],p["sym"],p["opened_ts"]-60,None,0,p["entry_mcap"]);COINS[p["mint"]]=c;CURVE[p["mint"]]=p["curve"];META[p["mint"]]=dict(sym=p["sym"],stage="held",series=[],last_reason=collections.Counter(),deep="ok")
    if not BOTS:seed_v10()
def ETday():
    from zoneinfo import ZoneInfo;return dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
def heldout_pool():
    r=json.load(open(f"{D}/bt_result.json"))["bots"]
    p=[x for x in r if x["genome"]["style"] in("sniper","momentum") and x["holdout"]["n"]>=25 and x["holdout"]["mean"]>0 and x["train"]["mean"]>0]
    p.sort(key=lambda x:-x["holdout"]["mean"]);return p
def v10_genome(x):
    g=dict(x["genome"]);g.update(size_pct=0.065,max_daily=8);return g
def held_note(x):return f"held-out n={x['holdout']['n']} {x['holdout']['mean']:+.1%}/trade, win {x['holdout']['win']:.0%} (train n={x['train']['n']} {x['train']['mean']:+.1%})"
def seed_v10():
    """v10: memecoin desk = every sniper/momentum genome positive on BOTH train and held-out tapes, equal split of 80% of $1,000."""
    c=CFG();P=heldout_pool();cap=c["start_equity"]*c["alloc"]["meme"]
    for i,x in enumerate(P):new_bot(v10_genome(x),0,f"heldout#{i}",cap/len(P),held_note(x))
    kvset("used_cfg",list(range(len(P))));kvset("desk_start",cap);kvset("desk_t0",time.time());kvset("epoch",0);kvset("epoch_t0",time.time());kvset("v6",1);kvset("v7",1)
def seed_v9():
    """v9: memecoin desk = ONE strict sniper, the best held-out sniper genome (train n41 +10.9%/trade, held-out n28 +13.5%/trade), 10% of $1,000."""
    c=CFG();r=json.load(open(f"{D}/bt_result.json"))["bots"]
    sn=[x for x in r if x["genome"]["style"]=="sniper" and x["holdout"]["n"]>=25 and x["holdout"]["mean"]>0 and x["train"]["mean"]>0]
    sn.sort(key=lambda x:-x["holdout"]["mean"]);x=sn[0];g=dict(x["genome"]);g.update(style="sniper_strict",max_daily=6,size_pct=0.2,max_early=15,social=1)
    new_bot(g,0,"v9 seed",c["start_equity"]*c["alloc"]["meme"],f"best held-out sniper: train n={x['train']['n']} {x['train']['mean']:+.1%}/trade, held-out n={x['holdout']['n']} {x['holdout']['mean']:+.1%}/trade; strict rug guard")
    kvset("desk_start",c["start_equity"]*c["alloc"]["meme"]);kvset("desk_t0",time.time());kvset("epoch",0);kvset("epoch_t0",time.time());kvset("v6",1);kvset("v7",1)
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
def migrate_v6():
    """Strategy switch requested by the owner: copy-trading desk + one strict all-checks sniper. Old bots retired (cash carried over)."""
    pool=0
    for b in list(BOTS.values()):
        for mint,p in list(b.pos.items()):
            c=COINS.get(mint);v=strat.sell_fill(c,p["tok"])*SOL[0] if c and SOL[0] else p["cost"]*0.5
            b.cash+=v;db.execute("update bot_pos set tok=0,proceeds=proceeds+?,status='closed',closed=?,exit_reason='retired for v6 copy desk' where id=?",(v,now(),p["id"]))
        pool+=b.cash;db.execute("update bots set status='retired',died=?,cash=0 where id=?",(now(),b.id));BOTS.pop(b.id)
    pool=pool or CFG()["start_equity"];rng=random.Random()
    r=json.load(open(f"{D}/bt_result.json")) if os.path.exists(f"{D}/bt_result.json") else {"bots":[]}
    sn=[x["genome"] for x in r["bots"] if x["genome"]["style"]=="sniper"]
    g=dict(sn[0]) if sn else strat.rand_genome("sniper",rng)
    g.update(style="sniper_strict",max_daily=4,size_pct=0.25,max_early=15,social=1)
    new_bot(g,0,"seed",pool/10,"strict sniper: every check must pass (RugCheck indexed, socials, dev<5%, snipers<15%, top10<35%)")
    for i in range(9):new_bot(strat.copy_genome(rng),0,"seed",pool/10,"copy-trader: smart-wallet filters + analyst kills")
    kvset("v6",time.time());kvset("v6_start",pool);STATS["replaced"]=0
    A["RISK"].say("EVOLVE",f"Desk switched to copy-trading: 9 copy bots + 1 strict sniper, ${pool:.2f} carried over",head=True)
def migrate_v7():
    """Data-driven reallocation (owner asked for profitability): copy-trading had no out-of-sample edge in any test, so it is defunded to one
    small observer bot. Capital goes to the backtest genomes that were positive on the HELD-OUT window (chosen by train score only)."""
    pool=0
    for b in list(BOTS.values()):
        for mint,p in list(b.pos.items()):
            c=COINS.get(mint);v=strat.sell_fill(c,p["tok"])*SOL[0] if c and SOL[0] else p["cost"]*0.5
            b.cash+=v;db.execute("update bot_pos set tok=0,proceeds=proceeds+?,status='closed',closed=?,exit_reason='v7 reallocation' where id=?",(v,now(),p["id"]))
        pool+=b.cash;db.execute("update bots set status='retired',died=?,cash=0 where id=?",(now(),b.id));BOTS.pop(b.id)
    r=json.load(open(f"{D}/bt_result.json"))["bots"]
    good=[x for x in r if x["holdout"]["n"]>=25 and x["holdout"]["mean"]>0.05 and x["train"]["mean"]>0]
    good.sort(key=lambda x:-x["train"]["mean"])
    rng=random.Random();alloc=[]
    for x in good[:4]:
        g=dict(x["genome"]);g["max_daily"]=8;g["size_pct"]=max(g["size_pct"],0.12)
        alloc.append((g,0.8/max(1,len(good[:4])),f"held-out backtest: n={x['holdout']['n']} mean {x['holdout']['mean']:+.1%} (train {x['train']['mean']:+.1%})"))
    sn=[x["genome"] for x in r if x["genome"]["style"]=="sniper"]
    g=dict(sn[0]) if sn else strat.rand_genome("sniper",rng);g.update(style="sniper_strict",max_daily=4,size_pct=0.2,max_early=15,social=1)
    alloc.append((g,0.12,"strict sniper (all checks)"))
    cg=strat.copy_genome(rng);cg.update(consensus=2,size_pct=0.15,max_daily=3)
    alloc.append((cg,0.08,"copy observer: copy-trading lost out-of-sample in every test, kept small to keep measuring"))
    for g,w,note in alloc:new_bot(g,0,"v7 seed",pool*w,note)
    kvset("v7",time.time());A["RISK"].say("EVOLVE",f"Reallocated ${pool:.2f}: {len(good[:4])} held-out-positive momentum/sniper bots (80%), strict sniper 12%, copy observer 8%",head=True)
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
        an.live("◆",f"{b.name} wants ${c.sym} · {b.style} · {c.mcap:.0f} SOL mc · {c.up}↑/{c.down}↓");chk(mint,"analyst",f"{b.name} ({b.style}) signal")
        asyncio.create_task(approve(b,mint))
    e=CHECK.get(mint)
    if e and e["verdict"] in("checking",) or (e and e["verdict"].startswith("no entry")):
        lr=m["last_reason"].most_common(1)
        if lr:e["verdict"]="no entry: "+lr[0][0]
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
    rg=rug_guard(c,m)
    if rg:chk(mint,verdict="rug guard: "+rg);KILLS["rug guard: "+rg.split(" (")[0]]+=1;return
    v=risk_veto(b,c)
    if v:A["RISK"].live("⛔",f"{b.name} ${c.sym} veto: {v}");chk(mint,"risk",v);return
    chk(mint,"risk","approved",verdict=f"→ {b.name} buying")
    DONE.add((b.id,mint));EXEC.order(b,mint,"buy",1.0,f"{b.style}: {c.up}↑/{c.down}↓, +{c.inflow:.1f} SOL in, {c.mcap:.0f} SOL mc, age {time.time()-c.born:.0f}s")
RUGGERS=set(kv("ruggers",[]) or [])
def rug_guard(c,m):
    """Strict rug guard applied to every bot: RugCheck indexed with mint+freeze revoked (deep_check), top10<=35%, dev<=5%, curve liquidity, dev's past coins."""
    if not m.get("rug_ok"):return "RugCheck not indexed"
    if c.dev_pct>5:return f"dev holds {c.dev_pct:.1f}% (>5%)"
    if c.dev_sold:return "dev sold"
    if m.get("top10") is None:return "top holders unknown"
    if c.vsol-30<CFG().get("min_liq_sol",3):return f"liquidity {max(0,c.vsol-30):.1f} SOL in curve (<{CFG().get('min_liq_sol',3)})"
    if c.creator and c.creator in RUGGERS:return "dev rugged a previous coin"
    return None
def mark_rugger(c):
    if c and c.creator and c.creator not in RUGGERS:RUGGERS.add(c.creator);kvset("ruggers",list(RUGGERS)[-5000:])
def meme_eq():return desk_equity()
def day_guard():
    d=ETday();st=kv("day_start",{}) or {}
    if st.get("d")!=d:st=dict(d=d,eq=meme_eq());kvset("day_start",st)
    return st
def risk_veto(b,c):
    st=day_guard()
    if meme_eq()<st["eq"]*0.9:return f"desk down {1-meme_eq()/st['eq']:.0%} today (ET) - paused until tomorrow"
    if os.path.exists(f"{DATA}/KILL") or CFG().get("kill_switch"):return "kill switch"
    if len(b.pos)>=CFG()["max_open_per_bot"]:return f"max {CFG()['max_open_per_bot']} open"
    eq=b.equity()
    if eq<b.start*CFG()["bot_floor_pct"]:return "bot below capital floor"
    if b.cash<1:return "no cash"
    today=dt.date.today().isoformat()
    if STATS["day_"+today]>=CFG().get("max_trades_day",50):return "desk daily trade budget used"
    hr=dt.datetime.now().strftime("%Y-%m-%dT%H")
    if HOURLY[hr]>=CFG().get("max_trades_hour",3):return "desk hourly pace (spreads ~50 trades over the day)"
    if b.day.get(today,0)>=round(b.g.get("max_daily",4)):return f"bot daily budget {round(b.g.get('max_daily',4))} used"
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
            usd_sz=min(b.cash,max(b.equity()*b.g["size_pct"],0.15*SOL[0]));sol=usd_sz/SOL[0]
            if sol<0.1:return
            tok=strat.buy_fill(c,sol)
            if tok<=0:return
            b.cash-=usd_sz
            cur=db.execute("insert into bot_pos(bot,mint,sym,curve,opened,opened_ts,entry_mcap,peak_mcap,tok,tok0,cost,status,reason,sol_px) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(b.id,mint,c.sym,CURVE.get(mint),now(),ts,c.mcap,c.mcap,tok,tok,usd_sz,"open",o["why"],SOL[0]))
            db.execute("update bots set cash=? where id=?",(b.cash,b.id))
            wl=sorted({x[1] for x in (c.flag_copy or []) if ts-x[0]<=b.g.get("window_s",600)}) if b.style=="copy" else []
            db.execute("update bot_pos set wallets=? where id=?",(json.dumps(wl),cur.lastrowid))
            b.pos[mint]=dict(id=cur.lastrowid,entry_mcap=c.mcap,peak_mcap=c.mcap,opened=ts,tp_done=False,tok=tok,cost=usd_sz,got=0.0,sym=c.sym,wallets=wl)
            STATS["day_"+dt.date.today().isoformat()]+=1;HOURLY[dt.datetime.now().strftime("%Y-%m-%dT%H")]+=1;b.day[dt.date.today().isoformat()]=b.day.get(dt.date.today().isoformat(),0)+1
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
                b.strikes=0 if pnl>0 else b.strikes+1;db.execute("update bots set strikes=? where id=?",(b.strikes,b.id))
                for w_ in p.get("wallets") or []:wallet_result(w_,pnl>0)
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
        if not r and b.style=="copy" and b.g.get("follow_sells")>=0.5 and any(SELLSIG[mint].get(w_,0)>p["opened"] for w_ in p.get("wallets") or []):r=("smart wallet sold",1)
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
        if holders(mint) or EXEC.q.get(mint) or (c.flag_copy and ts-c.flag_copy[-1][0]<900):continue
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
def leaders_for(excl):
    al=[x for x in BOTS.values() if x is not excl and x.strikes<3 and x.equity()>=x.start*0.85 and x.style==excl.style] or [x for x in BOTS.values() if x is not excl and x.strikes<3 and x.equity()>=x.start*0.85]
    return sorted(al,key=lambda x:-(x.equity()/max(x.start,1e-9)))[:3]
def replace_bot(b,why):
    """Liquidate b at market, spawn a mutation/crossover of the current leaders with b's remaining capital."""
    if b.id not in BOTS:return
    for mint in list(b.pos):
        if COINS.get(mint):EXEC.fill(dict(bot=b.id,side="sell",frac=1.0,why="bot replaced",t=0),mint,time.time())
    pool=b.equity();db.execute("update bots set status='replaced',died=?,cash=0,note=? where id=?",(now(),why,b.id));BOTS.pop(b.id)
    P=heldout_pool();used=kv("used_cfg",[]) or [];nxt=[i for i in range(len(P)) if i not in used]
    if nxt:
        i=nxt[0];kvset("used_cfg",used+[i]);nb=new_bot(v10_genome(P[i]),b.gen+1,f"heldout#{i}",pool,held_note(P[i]))
        nb.lessons=f"Replaces {b.name}, deleted for: {why}. Same deletion rules apply to me.";db.execute("update bots set lessons=? where id=?",(nb.lessons,nb.id))
        db.execute("insert into evolutions(ts,epoch,ranking,removed,added) values(?,?,?,?,?)",(now(),kv("epoch",0),json.dumps(why),json.dumps([b.name]),json.dumps([nb.name])));db.commit()
        A["RISK"].say("EVOLVE",f"{b.name} deleted ({why}) → {nb.name}, next held-out config, gets ${pool:.2f}",head=True);return
    if BOTS:
        best=max(BOTS.values(),key=lambda x:x.equity()/max(x.start,1e-9));best.cash+=pool;best.start+=pool
        db.execute("update bots set cash=?,start_cash=? where id=?",(best.cash,best.start,best.id))
        db.execute("insert into evolutions(ts,epoch,ranking,removed,added) values(?,?,?,?,?)",(now(),kv("epoch",0),json.dumps(why),json.dumps([b.name]),json.dumps([])));db.commit()
        A["RISK"].say("EVOLVE",f"{b.name} deleted ({why}). No untested held-out-positive config left, so ${pool:.2f} moved to {best.name}",head=True);return
    L=leaders_for(b);rng=random.Random()
    if len(L)>=2 and rng.random()<0.4:x,y=rng.sample(L,2);g=strat.mutate(strat.crossover(x.g,y.g,rng),rng,0.2,0.1);par=f"{x.name}×{y.name}";gen=max(x.gen,y.gen)+1
    elif L:x=L[0] if rng.random()<0.6 else rng.choice(L);g=strat.mutate(x.g,rng);par=x.name;gen=x.gen+1
    else:g=strat.mutate(b.g,rng,0.6,0.3);par=b.name+" (re-roll)";gen=b.gen+1
    if b.style=="sniper_strict" or b.style=="copy":
        if b.style=="copy" and g.get("style")!="copy":g=strat.copy_genome(rng)
        g["style"]=b.style
    tr=[dict(ret=(r_["proceeds"]-r_["cost"])/r_["cost"],entry_mcap=r_["entry_mcap"],held_s=(dt.datetime.fromisoformat(r_["closed"]).timestamp()-r_["opened_ts"]) if r_["closed"] else 0,exit=(r_["exit_reason"] or "").split("; ")[-1],wallets=json.loads(r_["wallets"] or "[]")) for r_ in db.execute("select * from bot_pos where bot=? and status='closed' and cost>0",(b.id,))]
    g["avoid_wallets"]=sorted(set(g.get("avoid_wallets",[]))|set(b.g.get("avoid_wallets",[])))
    g,notes=strat.lessons(g,tr)
    nb=new_bot(g,gen,par,pool,f"replaced {b.name}: {why}")
    nb.lessons=f"Learned from {b.name} ({why}): "+("; ".join(notes) if notes else "no clear pattern in its trades")
    db.execute("update bots set lessons=? where id=?",(nb.lessons,nb.id))
    STATS["replaced"]+=1;db.execute("insert into evolutions(ts,epoch,ranking,removed,added) values(?,?,?,?,?)",(now(),kv("epoch",0),json.dumps(why),json.dumps([b.name]),json.dumps([nb.name])));db.commit()
    A["RISK"].say("EVOLVE",f"{b.name} out ({why}) → {nb.name} born from {par} with ${pool:.2f}. {nb.lessons}",head=True)
def check_strikes(b):
    if b.id not in BOTS:return
    dd=1-b.equity()/max(b.start,1e-9)
    cl=b.closed();net=sum(r["proceeds"]-r["cost"] for r in cl)
    if dd>0.15:replace_bot(b,f"drawdown {dd:.0%} of its allocation (>15%)")
    elif len(cl)>=20 and net<0:replace_bot(b,f"{len(cl)} trades, net {'−' if net<0 else ''}${abs(net):.2f} after fees")
async def evolution():
    while True:
        await asyncio.sleep(30)
        for b in list(BOTS.values()):
            check_strikes(b)
        if time.time()-kv("epoch_t0",time.time())<CFG()["evolve_hours"]*3600:continue
        ep=kv("epoch",0)+1;kvset("epoch",ep);kvset("epoch_t0",time.time())
        el=[b for b in BOTS.values() if len(b.closed())>=20 and b.equity()<b.start]
        if el:
            w=min(el,key=lambda b:b.equity()/max(b.start,1e-9));replace_bot(w,f"daily cull: worst net-negative bot with 20+ trades")
        else:A["RISK"].say("EVOLVE",f"daily cull: no net-negative bot with 20+ trades - nobody deleted",head=True)
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
def contracts(bid,n=6):
    return [dict(mint=r["mint"],sym=r["sym"],pnl=round(r["proceeds"]-r["cost"],2) if r["status"]=="closed" else None,status=r["status"]) for r in db.execute("select mint,sym,proceeds,cost,status from bot_pos where bot=? order by id desc limit ?",(bid,n))]
def bots_view():
    out=[]
    for b in BOTS.values():
        cl=b.closed();w=sum(1 for r in cl if r["proceeds"]>r["cost"]);s,n=score(b)
        out.append(dict(id=b.id,name=b.name,gen=b.gen,style=b.style,parents=b.parents,equity=round(b.equity(),2),start=round(b.start,2),pnl=round(b.equity()-b.start,2),trades=len(cl),wins=w,open=len(b.pos),strikes=b.strikes,dd=round(100*(1-b.equity()/max(b.start,1e-9)),1),score=None if s is None else round(s,2),status="alive",contracts=contracts(b.id),lessons=b.lessons,today=b.day.get(dt.date.today().isoformat(),0),max_daily=round(b.g.get("max_daily",4)),size_pct=round(100*b.g["size_pct"]),
            g={k:(round(v,3) if isinstance(v,float) else v) for k,v in b.g.items() if k in("consensus","min_wret","max_lag_s","max_mcap","stop","trail","tp1","follow_sells")}))
    out.sort(key=lambda x:-x["pnl"])
    for r in db.execute("select id,name,gen,style,parents,died,note from bots where status='replaced' order by id desc limit 8"):out.append(dict(name=r["name"],gen=r["gen"],style=r["style"],parents=r["parents"],status="replaced",died=r["died"],why=(r["note"] or "")[:120],contracts=contracts(r["id"],4)))
    return out
def tape():
    xs=[c for m,c in COINS.items() if not META[m].get("killed") and len(c.hist)>2 and c.up>=3]
    xs.sort(key=lambda c:-(c.last_ts));out=[]
    for c in xs[:24]:out.append(dict(sym=c.sym,px=strat.mid(c)*SOL[0],ch=round((c.mcap/c.hist[0][1]-1)*100,1)))
    return out
def board():
    ts=time.time();hold=[];done=[]
    for b in BOTS.values():
        for mint,p in b.pos.items():
            c=COINS.get(mint);mc=c.mcap if c else p["entry_mcap"];val=p["tok"]*strat.mid(c)*SOL[0] if c else 0
            hold.append(dict(mint=mint,sym=p["sym"],bot=b.name,style=b.style,entry=round(p["entry_mcap"],1),mcap=round(mc,1),cost=round(p["cost"],2),value=round(val+p["got"],2),pnl=round(val+p["got"]-p["cost"],2),r=round(100*((val+p["got"])/p["cost"]-1),1),age=int(ts-p["opened"]),wallets=[TRACKED.get(w,w[:6]) for w in p.get("wallets") or []],stop=round(100*b.g["stop"]),tp_done=p["tp_done"]))
    for r in db.execute("select p.*,b.name bn,b.style bs from bot_pos p join bots b on b.id=p.bot where p.status='closed' order by p.id desc limit 14"):
        done.append(dict(id=r["id"],mint=r["mint"],sym=r["sym"],bot=r["bn"],style=r["bs"],entry=round(r["entry_mcap"],1),cost=round(r["cost"],2),pnl=round(r["proceeds"]-r["cost"],2),r=round(100*(r["proceeds"]/r["cost"]-1),1) if r["cost"] else 0,exit=(r["exit_reason"] or "").split("; ")[-1],why=r["reason"],closed=r["closed"],wallets=[TRACKED.get(w,w[:6]) for w in json.loads(r["wallets"] or "[]")]))
    chkl=[dict(e,age=int(ts-e["ts"])) for e in list(CHECK.values())[::-1] if ts-e["ts"]<300 and not holders(e["mint"])][:8]
    return dict(checking=chkl,holding=hold,done=done)
def stats():
    c=CFG();eq=desk_equity();st=kv("desk_start",c["start_equity"])
    return dict(start=st,equity=round(eq,4),pnl=round(eq-st,2),wins=STATS["wins"],losses=STATS["losses"],open=sum(len(b.pos) for b in BOTS.values()),
        seen=STATS["seen"],killed=STATS["killed"],signals=STATS["signals"],buys=STATS["buys"],closed=STATS["closed"],stops=STATS["stops"],updates=STATS["updates"],
        watching=len(COINS),sol=SOL[0],uptime=int(time.time()-START),t0=kv("desk_t0",START),epoch=kv("epoch",0),next_evolve=kv("epoch_t0",time.time())+c["evolve_hours"]*3600,
        feeds=dict(pumpportal=bool(WS[0]),rpc_poll=time.time()-WD["poll"]<10),profile=c.get("profile"),sub=c["subscription"],
        smart=len(SMART),copy_signals=STATS["copy_signals"],wdbg=dict(streams=STATS["w_streams"],drops=STATS["w_drops"],notif=STATS["w_notif"],pump=STATS["w_pump"],decoded=STATS["w_decoded"],txfail=STATS["w_txfail"],last=list(WDBG)[-5:]),today=STATS["day_"+dt.date.today().isoformat()],max_day=c.get("max_trades_day",50),replaced=STATS["replaced"])
def snapshot():
    return dict(board=board(),stats=stats(),agents=[a.view() for a in A.values()],server_now=time.time(),kills=KILLS.most_common(12),bots=bots_view(),tape=tape(),head=HEAD,
        equity=[list(r) for r in db.execute("select ts,equity from (select ts,equity from equity order by ts desc limit 1500) order by ts")],
        log=[dict(r) for r in db.execute("select ts,agent,tag,msg from events order by id desc limit 60")])
# ---------------- web ----------------
SECRET=hashlib.sha256(("sage:"+PW).encode()).digest()
def tok():e=str(int(time.time())+30*86400);return e+"."+hmac.new(SECRET,e.encode(),"sha256").hexdigest()
def valid(t):
    try:e,s=t.split(".");return int(e)>time.time() and hmac.compare_digest(s,hmac.new(SECRET,e.encode(),"sha256").hexdigest())
    except Exception:return False
PUBLIC={"/login","/manifest.json","/icon-192.png","/icon-512.png","/icon-maskable-512.png","/icon-1024.png","/favicon-32.png","/apple-touch-icon.png","/sw.js","/favicon.ico","/healthz"}
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
    ts=time.time();ok=ts-WD["loop"]<30 and ts-WD["poll"]<120 and (CD is None or ts-CD.ok<120 or ts-START<180)
    return web.Response(text="ok" if ok else "stalled",status=200 if ok else 503)
async def api_state(req):return web.json_response(snapshot(),headers={"Cache-Control":"no-store"})
async def api_backtest(req):
    p=f"{D}/bt_result.json";return web.json_response(json.load(open(p)) if os.path.exists(p) else {},headers={"Cache-Control":"no-store"})
async def api_equity(req):
    tf=req.query.get("tf","1d");sec=dict(h1=3600,h6=21600,d1=86400,w1=604800).get({"1h":"h1","6h":"h6","1d":"d1","1w":"w1"}.get(tf,""),10**9)
    since=(dt.datetime.now()-dt.timedelta(seconds=min(sec,10**8))).isoformat()
    rows=db.execute("select ts,equity from equity where ts>=? order by ts",(since,)).fetchall()
    step=max(1,len(rows)//500);pts=[[r[0],round(r[1],4)] for r in rows[::step]]
    if rows and pts[-1][0]!=rows[-1][0]:pts.append([rows[-1][0],round(rows[-1][1],4)])
    return web.json_response(pts,headers={"Cache-Control":"no-store"})
async def api_trades(req):
    return web.json_response([dict(r) for r in db.execute("select p.id,b.name,b.style,b.genome,p.sym,p.opened_ts,p.closed,p.entry_mcap,p.peak_mcap,p.cost,p.proceeds,p.status,p.reason,p.exit_reason,p.sol_px from bot_pos p join bots b on b.id=p.bot order by p.id")],headers={"Cache-Control":"no-store"})
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
            if time.time()-last>=2:await r.write(f"event: stats\ndata: {json.dumps(dict(board=board(),stats=stats(),server_now=time.time(),kills=KILLS.most_common(12),bots=bots_view(),tape=tape()))}\n\n".encode());last=time.time()
    except (ConnectionResetError,asyncio.CancelledError,Exception):pass
    finally:SUBS.discard(q)
    return r
# ---------------- v9: crypto desk + executive ----------------
import crypto
def _say(agent,tag,msg,pnl=None):A[agent].say(tag,msg,pnl=pnl,head=tag in("BUY","SELL","EVOLVE"))
def _live(agent,line):A[agent].live("·",line)
CD=None;BENCH={}
def totals():
    c=CFG();ce=CD.equity() if CD else 0;me=desk_equity()
    sol=CD.px.get("SOL") if CD else None
    if sol and not kv("bench_sol0"):kvset("bench_sol0",sol);kvset("bench_t0",time.time())
    s0=kv("bench_sol0");bench=c["start_equity"]*sol/s0 if sol and s0 else c["start_equity"]
    return dict(total=round(ce+me,4),crypto=round(ce,4),meme=round(me,4),bench=round(bench,4),start=c["start_equity"])
async def exec_loop():
    c=CFG();A["EXECUTIVE"].say("INFO",f"Allocation (owner's choice): {c['alloc']['meme']:.0%} memecoin desk ({len(BOTS)} held-out-positive bots, strict rug guard), {c['alloc']['crypto']:.0%} crypto trend backup. Caveat: memecoin held-out window is ~2h of tapes; crypto held-out is ~5 months.",head=True)
    last=0
    while True:
        t=totals()
        if time.time()-last>=15:db.execute("insert into equity2 values(?,?,?,?,?)",(time.time(),t["total"],t["crypto"],t["meme"],t["bench"]));db.commit();last=time.time()
        A["EXECUTIVE"].live("·",f"total ${t['total']:.2f} · crypto ${t['crypto']:.2f} · meme ${t['meme']:.2f} · hold-SOL ${t['bench']:.2f}")
        await asyncio.sleep(3)
def v9view():
    t=totals();c=CFG()
    return dict(t=t,alloc=c["alloc"],crypto=CD.view(),score=CD.scorecard(kv("bench_t0"),kv("bench_sol0")),meme=dict(bots=bots_view(),board=board(),stats=stats()),
        agents=[dict(a.view(),charter=crypto.CHARTERS.get(a.name,a.role)) for a in A.values()],server_now=time.time(),
        log=[dict(r) for r in db.execute("select ts,agent,tag,msg,pnl from events order by id desc limit 80")])
def stopline(b,p):
    st=p["entry_mcap"]*(1+b.g["stop"])
    if p["tp_done"]:st=max(st,p["peak_mcap"]*(1-b.g["trail"]))
    return st
def v10view():
    v=v9view();ts=time.time()
    for h in v["meme"]["board"]["holding"]:
        b=next((x for x in BOTS.values() if h["mint"] in x.pos),None)
        if b:p=b.pos[h["mint"]];h["stop_mcap"]=round(stopline(b,p),2);h["stack_pct"]=round(100*p["cost"]/max(b.equity(),1e-9),1);h["trail"]=b.g["trail"];h["tp1"]=b.g["tp1"]
    st=day_guard();me=meme_eq()
    v["v10"]=dict(stats=dict(seen=STATS["seen"],killed=STATS["killed"],signals=STATS["signals"],buys=STATS["buys"],closed=STATS["closed"],watching=len(COINS),passed=sum(1 for m in META.values() if m.get("deep")=="ok"),scoring=sum(1 for m in META.values() if m.get("deep")=="pending")),
        kills=KILLS.most_common(8),recent_kills=[dict(sym=m.get("sym"),why=m.get("killed")) for m in list(META.values())[-60:] if m.get("killed")][-6:][::-1],
        day=dict(d=st["d"],start=round(st["eq"],2),now=round(me,2),paused=me<st["eq"]*0.9),
        rules=dict(desc=["stop set before entry (per bot genome)","partial take at TP1, then trailing stop","stake ~6.5% of bot stack per coin","desk pauses if down 10% on the ET day","delete bot: >=20 trades & net loss, or drawdown >15%","daily cull: worst net-negative bot with >=20 trades"]),
        last_trail=next((dict(r) for r in db.execute("select sym,proceeds-cost pnl,exit_reason,closed from bot_pos where status='closed' and (exit_reason like '%trail%' or exit_reason like '%stop%') order by id desc limit 1")),None),
        watch=[dict(sym=c.sym,mint=m,mcap=round(c.mcap*SOL[0]),ch=round((c.mcap/c.hist[0][1]-1)*100,1) if c.hist else 0) for m,c in list(COINS.items())[-200:] if not META[m].get("killed") and META[m].get("deep")=="ok"][-8:][::-1],
        sol=SOL[0],ruggers=len(RUGGERS))
    return v
async def api_v10(req):return web.json_response(v10view(),headers={"Cache-Control":"no-store"})
async def api_token(req):
    mint=req.query.get("mint","");c=COINS.get(mint);m=META.get(mint)
    r=db.execute("select p.*,b.name bn,b.genome g from bot_pos p join bots b on b.id=p.bot where p.mint=? order by p.id desc limit 1",(mint,)).fetchone()
    ser=(m or {}).get("series") or []
    if not ser:
        t=db.execute("select series from ticks where mint=?",(mint,)).fetchone();ser=json.loads(t[0]) if t else []
    out=dict(mint=mint,sym=(m or {}).get("sym") or (r["sym"] if r else mint[:4]),chain="Solana · pump.fun",born=c.born if c else None,sol=SOL[0],
        mcap=c.mcap if c else None,liq=(c.vsol-30) if c else None,top10=(m or {}).get("top10"),series=ser[-600:],buys=c.up if c else None,sells=c.down if c else None,vol=(c.inflow+c.outflow) if c else None)
    if r:
        g=json.loads(r["g"]);b=BOTS.get(r["bot"]);p=b.pos.get(mint) if b else None
        out["pos"]=dict(bot=r["bn"],status=r["status"],entry=r["entry_mcap"],cost=r["cost"],proceeds=r["proceeds"],opened=r["opened_ts"],exit=r["exit_reason"],
            stop=stopline(b,p) if p else r["entry_mcap"]*(1+g["stop"]),value=(p["tok"]*strat.mid(c)*SOL[0]+p["got"]) if (p and c) else r["proceeds"],stop_pct=g["stop"],trail=g["trail"],tp1=g["tp1"])
    return web.json_response(out,headers={"Cache-Control":"no-store"})
async def api_v9(req):return web.json_response(v9view(),headers={"Cache-Control":"no-store"})
async def api_eq2(req):
    sec={"1h":3600,"6h":21600,"1d":86400,"1w":604800}.get(req.query.get("tf","1d"),10**10)
    rows=db.execute("select ts,total,bench,crypto,meme from equity2 where ts>=? order by ts",(time.time()-sec,)).fetchall()
    step=max(1,len(rows)//400);pts=[list(r) for r in rows[::step]]
    if rows and pts[-1][0]!=rows[-1][0]:pts.append(list(rows[-1]))
    return web.json_response(pts,headers={"Cache-Control":"no-store"})
async def main():
    HTTP[0]=aiohttp.ClientSession(headers={"User-Agent":"Mozilla/5.0 (sage paper terminal)"})
    load_bots()
    global CD;CD=crypto.Desk(db,_say,_live,D)
    if not CD.cfgs():CD.seed(CFG()["start_equity"]*CFG()["alloc"]["crypto"])
    # close legacy v4 single-bot positions (no live state for them anymore)
    db.execute("update positions set status='closed',closed=?,exit_reason='v5 migration' where status='open'",(now(),));db.commit()
    app=web.Application(middlewares=[auth])
    app.router.add_get("/healthz",healthz)
    app.router.add_get("/login",static("login.html"));app.router.add_post("/login",login_post);app.router.add_get("/logout",logout)
    for n in ("manifest.json","icon-192.png","icon-512.png","icon-maskable-512.png","icon-1024.png","favicon-32.png","avatar.png","apple-touch-icon.png","favicon.ico"):app.router.add_get("/"+n,static(n))
    app.router.add_get("/sw.js",static("sw.js","application/javascript"))
    app.router.add_get("/",static("index.html"));app.router.add_get("/api/state",api_state);app.router.add_get("/api/stream",api_stream)
    app.router.add_get("/api/backtest",api_backtest);app.router.add_get("/api/evolutions",api_evolutions);app.router.add_get("/api/trades",api_trades);app.router.add_get("/api/equity",api_equity);app.router.add_get("/api/v9",api_v9);app.router.add_get("/api/v10",api_v10);app.router.add_get("/api/token",api_token);app.router.add_get("/api/eq2",api_eq2)
    run=web.AppRunner(app);await run.setup();await web.TCPSite(run,"0.0.0.0",int(os.environ.get("PORT","8787"))).start()
    A["RISK"].say("INFO",f"engine v9 up: {len(BOTS)} paper bots, epoch {kv('epoch',0)}",head=True)
    await asyncio.gather(hub(),poller(),CD.bar_loop(HTTP[0]),CD.exit_loop(HTTP[0]),exec_loop(),dev_watch(),risk_clock(),evolution(),watchdog(),sol_price(),flusher(),EXEC.sweeper())
if __name__=="__main__":asyncio.run(main())

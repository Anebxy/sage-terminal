"""PAPER TRADING ONLY. No wallets, keys, or transactions. Reads public APIs only."""
import json,time,sqlite3,threading,traceback,requests,os,datetime as dt
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
D=os.path.dirname(os.path.abspath(__file__))
DATA=os.environ.get("DATA_DIR") or D
try:os.makedirs(DATA,exist_ok=True);open(f"{DATA}/.w","w").close()
except Exception:DATA=D
DB=f"{DATA}/paper.db"
C=lambda:json.load(open(f"{D}/config.json"))
S=requests.Session();S.headers["User-Agent"]="paper-terminal/1.0";_last={}
def get(url,host_gap=1.2):
    h=url.split("/")[2];w=_last.get(h,0)+host_gap-time.time()
    if w>0:time.sleep(w)
    _last[h]=time.time()
    for i in range(3):
        try:
            r=S.get(url,timeout=15)
            if r.status_code==429:time.sleep(5*(i+1));continue
            r.raise_for_status();return r.json()
        except Exception as e:err=e;time.sleep(2)
    log("api_error",{"url":url,"err":str(err)[:200]});return None
def db():
    c=sqlite3.connect(DB,timeout=30);c.row_factory=sqlite3.Row;return c
def init():
    c=db();c.executescript("""create table if not exists events(id integer primary key,ts text,kind text,data text);
    create table if not exists signals(id integer primary key,ts text,token text,symbol text,pair text,source text,liq real,score real,status text,reason text);
    create table if not exists positions(id integer primary key,token text,symbol text,pair text,opened text,closed text,entry real,qty real,cost real,peak real,last real,remaining real,proceeds real default 0,fees real default 0,status text,exit_reason text,tp_hit int default 0);
    create table if not exists equity(ts text,equity real,cash real);""");c.commit()
def now():return dt.datetime.now().isoformat(timespec="seconds")
_conn=None
def log(kind,data):
    try:c=_conn or db();c.execute("insert into events(ts,kind,data) values(?,?,?)",(now(),kind,json.dumps(data)));c.commit()
    except Exception:pass
# ---------- scanner ----------
def candidates():
    out={}
    for src,url in [("profiles","https://api.dexscreener.com/token-profiles/latest/v1"),("boosts","https://api.dexscreener.com/token-boosts/latest/v1"),("boosts_top","https://api.dexscreener.com/token-boosts/top/v1")]:
        for t in get(url) or []:
            if t.get("chainId")=="solana":out.setdefault(t["tokenAddress"],src)
    g=get("https://api.geckoterminal.com/api/v2/networks/solana/new_pools",2.5)
    for p in (g or {}).get("data",[]):
        a=p["relationships"]["base_token"]["data"]["id"].split("_",1)[1];out.setdefault(a,"gecko_new")
    return out
def pairs_for(tokens):
    res={}
    for i in range(0,len(tokens),30):
        for p in get("https://api.dexscreener.com/tokens/v1/solana/"+",".join(tokens[i:i+30])) or []:
            a=p["baseToken"]["address"]
            if a in tokens and (a not in res or (p.get("liquidity") or {}).get("usd",0)>(res[a].get("liquidity") or {}).get("usd",0)):res[a]=p
    return res
# ---------- rug filter ----------
def rug(addr,p,cfg):
    liq=(p.get("liquidity") or {}).get("usd",0) or 0
    if liq<cfg["min_liq"]:return f"liquidity ${liq:,.0f} < ${cfg['min_liq']:,}"
    ch=(p.get("priceChange") or {}).get("h1",0) or 0
    if ch>cfg["max_1h_change_pct"]:return f"already up {ch:.0f}% in 1h"
    r=get(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report",2.0)
    if not r:return "rugcheck unavailable (fail-closed)"
    if r.get("rugged"):return "rugcheck: rugged"
    if r.get("mintAuthority"):return "mint authority not revoked"
    if r.get("freezeAuthority"):return "freeze authority not revoked"
    known=set((r.get("knownAccounts") or {}).keys());mk=set()
    for m in r.get("markets") or []:
        for k in ("pubkey","liquidityA","liquidityB","liquidityAAccount","liquidityBAccount"):
            if isinstance(m.get(k),str):mk.add(m[k])
    hs=[h for h in r.get("topHolders") or [] if h.get("owner") not in known and h.get("address") not in known and h.get("owner") not in mk and h.get("address") not in mk]
    top10=sum(h.get("pct",0) for h in hs[:10])
    if top10>cfg["max_top10_pct"]:return f"top10 holders {top10:.1f}% > {cfg['max_top10_pct']}%"
    sup=(r.get("token") or {}).get("supply") or 0;dec=(r.get("token") or {}).get("decimals") or 0
    dev=100*(r.get("creatorBalance") or 0)/sup if sup else 0
    if dev>cfg["max_dev_pct"]:return f"dev holds {dev:.1f}% > {cfg['max_dev_pct']}%"
    lp=max([(m.get("lp") or {}).get("lpLockedPct",0) or 0 for m in r.get("markets") or []]+[0])
    if lp<cfg["require_lp_locked_pct"]:return f"LP locked {lp:.0f}% < {cfg['require_lp_locked_pct']}%"
    if r.get("graphInsidersDetected",0)>=5:return f"insider/bundle network detected ({r['graphInsidersDetected']})"
    for k in r.get("risks") or []:
        if k.get("level")=="danger":return "rugcheck danger: "+k.get("name","")
    return None
# ---------- scorer ----------
def score(p):
    v=p.get("volume") or {};tx=(p.get("txns") or {}).get("h1",{});liq=(p.get("liquidity") or {}).get("usd",0) or 0
    m5,h1=v.get("m5",0) or 0,v.get("h1",0) or 0
    mom=min(30,30*(m5*12/h1)/2) if h1 else 0                 # m5 run-rate vs h1 (2x => full)
    b,s=tx.get("buys",0),tx.get("sells",0);bs=min(25,25*(b/max(s,1)-0.8)/0.7) if b+s>20 else 0
    lq=min(25,25*liq/150000)
    age=(time.time()*1000-(p.get("pairCreatedAt") or 0))/3.6e6
    ag=20 if 0.25<=age<=24 else 10 if age<=72 else 0
    return round(max(0,mom)+max(0,bs)+lq+ag,1),dict(mom=round(mom,1),bs=round(bs,1),liq=round(lq,1),age_h=round(age,1))
# ---------- copy-trading (configurable list; no PnL fabricated) ----------
def copy_watch(cfg):
    for w in cfg.get("copy_wallets",[]):
        try:
            r=S.post(cfg["solana_rpc"],json={"jsonrpc":"2.0","id":1,"method":"getSignaturesForAddress","params":[w,{"limit":10}]},timeout=15).json()
            log("copy_wallet_activity",{"wallet":w,"recent_sigs":len(r.get("result",[]))});time.sleep(2)
        except Exception as e:log("api_error",{"rpc":str(e)})
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
    log("exit" if closed else "partial_exit",{"symbol":p["symbol"],"price":px,"frac":frac,"reason":reason,"pnl":None if not closed else None})
def manage(c,cfg,live):
    for p in c.execute("select * from positions where status='open'").fetchall():
        pr=live.get(p["token"]);
        if not pr:continue
        peak=max(p["peak"],pr);c.execute("update positions set last=?,peak=? where id=?",(pr,peak,p["id"]))
        p=c.execute("select * from positions where id=?",(p["id"],)).fetchone();r=pr/p["entry"]-1
        age=(dt.datetime.now()-dt.datetime.fromisoformat(p["opened"])).total_seconds()/60
        if r<=cfg["stop_loss"]:sell(c,p,1,pr,f"stop loss {r:.1%}",cfg);continue
        if p["tp_hit"]>0 and pr<=peak*(1-cfg["trail_pct"]):sell(c,p,1,pr,f"trailing stop ({r:+.1%})",cfg);continue
        if age>=cfg["max_hold_min"]:sell(c,p,1,pr,f"time exit {age:.0f}m ({r:+.1%})",cfg);continue
        lad=cfg["tp_ladder"]
        if p["tp_hit"]<len(lad) and r>=lad[p["tp_hit"]][0]:
            sell(c,p,lad[p["tp_hit"]][1],pr,f"TP{p['tp_hit']+1} +{r:.0%}",cfg);c.execute("update positions set tp_hit=tp_hit+1 where id=?",(p["id"],))
def day_pnl(c,cfg):
    t=dt.date.today().isoformat();r=c.execute("select equity from equity where ts<? order by ts desc limit 1",(t,)).fetchone()
    base=r[0] if r else cfg["start_equity"];return state(c,cfg)[2]-base,base
def cycle():
    global _conn
    cfg=C();c=db();_conn=c
    cands=candidates()
    op=[p["token"] for p in c.execute("select token from positions where status='open'")]
    seen={r[0] for r in c.execute("select token from signals where ts>?",((dt.datetime.now()-dt.timedelta(hours=6)).isoformat(),))}
    pairs=pairs_for(list(set(cands)|set(op)))
    live={a:float(p["priceUsd"]) for a,p in pairs.items() if p.get("priceUsd")}
    manage(c,cfg,live);c.commit()
    for a,src in cands.items():
        if a in seen or a in op:continue
        p=pairs.get(a)
        def sig(st,rs,sc=None):
            c.execute("insert into signals(ts,token,symbol,pair,source,liq,score,status,reason) values(?,?,?,?,?,?,?,?,?)",(now(),a,(p or {}).get("baseToken",{}).get("symbol","?"),(p or {}).get("url",""),src,((p or {}).get("liquidity") or {}).get("usd"),sc,st,rs))
            log(st,{"symbol":(p or {}).get("baseToken",{}).get("symbol","?"),"reason":rs,"score":sc});c.commit()
        if not p:sig("killed","no DEX pair data");continue
        try:k=rug(a,p,cfg)
        except Exception as e:k=f'rug filter error (fail-closed): {e}'
        if k:sig("killed",k);continue
        sc,parts=score(p)
        if sc<cfg["min_score"]:sig("killed",f"score {sc} < {cfg['min_score']} {parts}",sc);continue
        cash,opn,eq=state(c,cfg);dp,base=day_pnl(c,cfg)
        if cfg["kill_switch"] or os.path.exists(f"{DATA}/KILL"):sig("passed_not_traded","kill switch on",sc);continue
        if dp<=-cfg["daily_loss_limit_pct"]*base:sig("passed_not_traded",f"daily loss limit hit ({dp:.2f})",sc);continue
        if len(opn)>=cfg["max_open"]:sig("passed_not_traded","max open positions",sc);continue
        size=min(cash,eq*cfg["size_pct"]);fee=size*cfg["fee_pct"];px=float(p["priceUsd"])*(1+cfg["slippage"])
        c.execute("insert into positions(token,symbol,pair,opened,entry,qty,cost,peak,last,remaining,fees,status) values(?,?,?,?,?,?,?,?,?,?,?,'open')",
          (a,p["baseToken"]["symbol"],p.get("url"),now(),px,(size-fee)/px,size,px,px,(size-fee)/px,fee))
        sig("traded",f"entry ${size:.2f} @ {px:.8g} score {sc} {parts}",sc);log("entry",{"symbol":p["baseToken"]["symbol"],"size":size,"price":px})
    cash,opn,eq=state(c,cfg);c.execute("insert into equity values(?,?,?)",(now(),eq,cash));c.commit()
    copy_watch(cfg);c.commit();_conn=None;write_status()
def stats():
    c=db();cfg=C();cash,op,eq=state(c,cfg)
    f={r[0]:r[1] for r in c.execute("select status,count(*) from signals group by status")}
    cl=c.execute("select * from positions where status='closed'").fetchall()
    w=sum(1 for p in cl if p["proceeds"]>p["cost"]);rp=sum(p["proceeds"]-p["cost"] for p in cl)
    kr=[dict(r) for r in c.execute("select substr(reason,1,instr(reason||' ',' ')+8) k,count(*) n from signals where status='killed' group by k order by n desc limit 8")]
    return dict(ts=now(),equity=round(eq,2),cash=round(cash,2),pnl=round(eq-cfg["start_equity"],2),funnel=dict(signals=sum(f.values()),**f),
      closed=len(cl),wins=w,win_rate=round(w/len(cl),3) if cl else None,realized_pnl_after_fees=round(rp,2),fees=round(sum(p["fees"] for p in c.execute("select fees from positions")),2),
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
        if not PW:return False
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
              feed=[dict(r) for r in c.execute("select ts,kind,data from events where kind!='api_error' order by id desc limit 80")],
              trades=[dict(r) for r in c.execute("select * from positions order by id desc limit 50")],
              signals=[dict(r) for r in c.execute("select ts,symbol,source,liq,score,status,reason,pair from signals order by id desc limit 80")]))
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

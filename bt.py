"""Replay backtester on real pump.fun tapes (GeckoTerminal trades). Same strat.py logic + curve fill model as live.
Chronological split: earliest 70% of launches = TRAIN (optimize), latest 30% = HOLDOUT (report)."""
import json,glob,sys,os,math,random,datetime as dt,pickle,multiprocessing as mp
sys.path.insert(0,os.path.dirname(__file__));import strat
from strat import K
class Snap:
    __slots__=("ts","mcap","vsol","vtok","up","down","inflow","last_up","peak","low_since_peak","ch10","ch30","early_pct","dev_sold","graduated","born","dev_pct","social_ok","flag_copy","hard_kill")
    def change(s,ts,secs):return s.ch10 if secs<=10 else s.ch30
    def age(s,ts):return ts-s.born
def load(path):
    d=json.load(open(path));c=d["coin"];tr=d.get("trades") or []
    if not tr:return None
    born=c["created_timestamp"]/1000;mint=c["mint"]
    if (c.get("quote_mint") or "1111") not in ("11111111111111111111111111111111","So11111111111111111111111111111111111111112"):return None   # non-SOL quote
    ev=[]
    for x in tr:
        t=dt.datetime.fromisoformat(x["block_timestamp"].replace("Z","+00:00")).timestamp()
        if x["kind"]=="buy":tok=float(x["to_token_amount"]);sol=float(x["from_token_amount"])
        else:tok=-float(x["from_token_amount"]);sol=-float(x["to_token_amount"])
        if abs(tok)<1:continue
        ev.append((t,tok,x["tx_from_address"],x["kind"],abs(sol)/abs(tok)*1e9))   # execution price -> mcap in SOL
    ev.sort()
    if len(tr)>=300 and ev[0][0]-born>5:return {"mint":mint,"trunc":True,"born":born}
    social=bool(c.get("twitter") or c.get("website") or c.get("telegram"))
    dev_tok=sum(e[1] for e in ev if e[2]==c["creator"] and e[3]=="buy" and e[0]-born<2)
    coin=strat.Coin(mint,c["symbol"],born,c["creator"],100*dev_tok/1e9,28.0,social)
    vtok=1.073e9;snaps=[];i=0
    # 1-second buckets
    while i<len(ev):
        b=math.floor(ev[i][0]);dev_sell=False
        while i<len(ev) and math.floor(ev[i][0])==b:
            vtok-=ev[i][1];mc=ev[i][4]
            if ev[i][2]==c["creator"] and ev[i][3]=="sell":dev_sell=True
            i+=1
        ts=b+1;vs=strat.vs_from_mcap(mc)
        coin.tick(ts,vs,K/vs)
        if dev_sell:coin.dev_sold=True
        s=Snap()
        for a in ("mcap","vsol","vtok","up","down","inflow","last_up","peak","low_since_peak","early_pct","dev_sold","graduated","born","dev_pct","social_ok","flag_copy","hard_kill"):setattr(s,a,getattr(coin,a))
        s.ts=ts;s.ch10=coin.change(ts,10);s.ch30=coin.change(ts,30);snaps.append(s)
    return {"mint":mint,"sym":c["symbol"],"born":born,"trunc":False,"snaps":snaps,"ath_usd":c.get("ath_market_cap")}
def sim_coin(g,cd,sol_size):
    """One coin, one bot. Decide on snapshot i; fill on snapshot i+1 (>=1s latency, after the next second of real trades)."""
    sn=cd["snaps"];pos=None;pend=None;trades=[];killed=False
    for i,s in enumerate(sn):
        if pend:
            kind,frac=pend;pend=None
            if kind=="buy":
                tok=strat.buy_fill(s,sol_size)
                pos={"entry_mcap":s.mcap,"peak_mcap":s.mcap,"opened":s.ts,"tp_done":False,"tok":tok,"cost":sol_size,"got":0.0}
            else:
                q=pos["tok"]*frac;pos["got"]+=strat.sell_fill(s,q);pos["tok"]-=q
                if frac>=0.999 or pos["tok"]<=1:trades.append(pos["got"]/pos["cost"]-1);return trades
                pos["tp_done"]=True
        if pos:
            # time-based checks at this moment with prior state already covered by exit_reason on current snapshot
            r=strat.exit_reason(g,s,pos,s.ts)
            if r:pend=("sell",r[1])
        elif not killed:
            if strat.hard_kill(s,s.ts):killed=True;continue
            if strat.wants_entry(g,s,s.ts) is None:pend=("buy",1)
    if pos:   # coin went quiet: close at last state (time exit), honest final mark
        pos["got"]+=strat.sell_fill(sn[-1],pos["tok"]);trades.append(pos["got"]/pos["cost"]-1)
    return trades
def evaluate(g,coins,sol_size=0.15):
    rs=[]
    for cd in coins:rs+=sim_coin(g,cd,sol_size)
    n=len(rs)
    if n==0:return dict(n=0,mean=0,sum=0,score=-9,win=0,med=0)
    m=sum(rs)/n;sd=math.sqrt(sum((x-m)**2 for x in rs)/max(n-1,1))+1e-9
    score=m/sd*math.sqrt(n) if n>=30 else -5+n/10     # risk-adjusted t-stat, min trade count
    srt=sorted(rs)
    return dict(n=n,mean=m,sum=sum(rs),score=score,win=sum(x>0 for x in rs)/n,med=srt[n//2],worst=srt[0],best=srt[-1])
_C=None
def _init(path):
    global _C;_C=pickle.load(open(path,"rb"))
def _ev(g):return evaluate(g,_C)
if __name__=="__main__":
    files=sorted(glob.glob("/workspace/bt/data/*.json"))
    with mp.Pool(os.cpu_count()) as p:cs=[c for c in p.map(load,files) if c]
    full=[c for c in cs if not c["trunc"]];trunc=[c for c in cs if c["trunc"]]
    full.sort(key=lambda c:c["born"]);cut=int(len(full)*0.7);train,hold=full[:cut],full[cut:]
    print(f"tapes {len(cs)} usable {len(full)} truncated(excluded) {len(trunc)}  train {len(train)} holdout {len(hold)}")
    print("train window",dt.datetime.fromtimestamp(train[0]["born"]).strftime("%H:%M"),"-",dt.datetime.fromtimestamp(train[-1]["born"]).strftime("%H:%M"),
          " holdout",dt.datetime.fromtimestamp(hold[0]["born"]).strftime("%H:%M"),"-",dt.datetime.fromtimestamp(hold[-1]["born"]).strftime("%H:%M"))
    pickle.dump(train,open("/tmp/train.pkl","wb"));pickle.dump(hold,open("/tmp/hold.pkl","wb"))
    rng=random.Random(7);GENS=int(os.environ.get("GENS",25));POP=int(os.environ.get("POP",120))
    cur=json.load(open("/workspace/paper-terminal-deploy/config.json"))
    base=dict(min_age=20,max_age=180,min_up=10,min_mcap=33,max_mcap=120,min_inflow=3,min_ratio=1.5,min_from_peak=0.85,max_avg_up=2.5,mom10=0.0,dip_dd=0.4,rebound=0.1,fresh_s=10,max_early=25,
              stop=-0.12,tp1=0.3,tp1_frac=0.33,trail=0.12,fade_s=45,fade_r=0.05,max_hold=900,size_pct=0.02,social=0,style="momentum")
    pop=[base]+[strat.rand_genome(st,rng) for st in ["sniper","momentum","dip"]*(POP//3)]
    with mp.Pool(os.cpu_count(),_init,("/tmp/train.pkl",)) as p:
        for gen in range(GENS):
            res=p.map(_ev,pop);order=sorted(range(len(pop)),key=lambda i:-res[i]["score"])
            b=res[order[0]];print(f"gen {gen} best train score {b['score']:.2f} n={b['n']} mean {b['mean']:+.3f} win {b['win']:.0%} style {pop[order[0]]['style']}",flush=True)
            elite=[pop[i] for i in order[:POP//5]]
            # keep per-style elites so the population stays diverse
            for st in ["sniper","momentum","dip"]:
                bs=[pop[i] for i in order if pop[i]["style"]==st][:3];elite+= [x for x in bs if x not in elite]
            nxt=list(elite)
            while len(nxt)<POP:
                a,b2=rng.sample(elite,2);nxt.append(strat.mutate(strat.crossover(a,b2,rng),rng))
            pop=nxt
        res=p.map(_ev,pop)
    order=sorted(range(len(pop)),key=lambda i:-res[i]["score"])
    # choose 10 by TRAIN score only (diverse styles), then report them on HOLDOUT
    chosen=[];
    for st in ["momentum","sniper","dip"]:chosen+=[pop[i] for i in order if pop[i]["style"]==st][:3]
    for i in order:
        if len(chosen)>=9:break
        if pop[i] not in chosen:chosen.append(pop[i])
    chosen=[base]+chosen[:9]
    with mp.Pool(os.cpu_count(),_init,("/tmp/hold.pkl",)) as p:hr=p.map(_ev,chosen)
    with mp.Pool(os.cpu_count(),_init,("/tmp/train.pkl",)) as p:tr=p.map(_ev,chosen)
    out=[]
    for g,a,b in zip(chosen,tr,hr):
        out.append({"genome":g,"train":a,"holdout":b});print(f"{g['style']:9s} TRAIN n={a['n']:4d} mean {a['mean']:+.3f} win {a['win']:.0%} | HOLDOUT n={b['n']:4d} mean {b['mean']:+.3f} win {b['win']:.0%} sum {b['sum']:+.2f}")
    json.dump({"train":len(train),"holdout":len(hold),"truncated":len(trunc),"bots":out,"ts":dt.datetime.now().isoformat()},open("/workspace/sage3/bt_result.json","w"),indent=1)

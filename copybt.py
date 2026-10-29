"""Copy-trading backtest on real pump.fun tapes. Smart wallets are picked ONLY from the train window,
then copying them is simulated on the later holdout window (out-of-sample)."""
import json,glob,math,os,sys,datetime as dt,collections,random,multiprocessing as mp,statistics as S
sys.path.insert(0,os.path.dirname(__file__));import strat,bt
def load2(path):
    d=json.load(open(path));c=d["coin"];tr=d.get("trades") or []
    cd=bt.load(path)
    if not cd or cd.get("trunc"):return None
    ev=[]
    for x in tr:
        t=dt.datetime.fromisoformat(x["block_timestamp"].replace("Z","+00:00")).timestamp()
        if x["kind"]=="buy":ev.append((t,x["tx_from_address"],float(x["from_token_amount"]),"b"))
        else:ev.append((t,x["tx_from_address"],float(x["to_token_amount"]),"s"))
    cd["ev"]=sorted(ev);cd["creator"]=c["creator"];return cd
def wallet_stats(coins):
    W=collections.defaultdict(lambda:dict(coins=0,pnl=0.0,wins=0,early=0))
    for cd in coins:
        per=collections.defaultdict(lambda:[0.0,0.0,None])
        for t,w,sol,k in cd["ev"]:
            if w==cd["creator"]:continue
            p=per[w]
            if k=="b":p[0]+=sol;p[2]=p[2] or t
            else:p[1]+=sol
        for w,(b,s,t1) in per.items():
            if b<=0.01:continue
            st=W[w];st["coins"]+=1;st["pnl"]+=s-b;st["wins"]+=s>b*1.05
    return W
def pick(W,min_coins,min_win,min_pnl,max_coins):
    return {w for w,s in W.items() if min_coins<=s["coins"]<=max_coins and s["wins"]/s["coins"]>=min_win and s["pnl"]>=min_pnl}
def sim_copy(g,cd,smart,sol=0.3,lag=2.0):
    sn=cd["snaps"];buys=[(t,w) for t,w,s,k in cd["ev"] if k=="b" and w in smart]
    if not buys:return []
    pos=None;pend=None;trades=[];bi=0;seen=[]
    for i,s in enumerate(sn):
        if pend:
            kind,frac=pend;pend=None
            if kind=="buy":tok=strat.buy_fill(s,sol);pos={"entry_mcap":s.mcap,"peak_mcap":s.mcap,"opened":s.ts,"tp_done":False,"tok":tok,"cost":sol,"got":0.0}
            else:
                q=pos["tok"]*frac;pos["got"]+=strat.sell_fill(s,q);pos["tok"]-=q
                if frac>=0.999:trades.append(pos["got"]/pos["cost"]-1);return trades
                pos["tp_done"]=True
        while bi<len(buys) and buys[bi][0]+lag<=s.ts:seen.append(buys[bi]);bi+=1
        if pos:
            r=strat.exit_reason(g,s,pos,s.ts)
            if r:pend=("sell",r[1])
        elif seen:
            if strat.hard_kill(s,s.ts):return trades
            rec={w for t,w in seen if s.ts-t<=g["window_s"]}
            if len(rec)>=round(g["consensus"]) and s.ts-seen[-1][0]<=g["max_lag_s"]+lag and s.mcap<=g["max_mcap"] and s.mcap>=27.5:pend=("buy",1)
    if pos:pos["got"]+=strat.sell_fill(sn[-1],pos["tok"]);trades.append(pos["got"]/pos["cost"]-1)
    return trades
def summ(rs):
    if not rs:return dict(n=0,mean=0,win=0,sum=0)
    return dict(n=len(rs),mean=round(S.mean(rs),4),win=round(sum(r>0 for r in rs)/len(rs),3),sum=round(sum(rs),3),med=round(S.median(rs),3))
if __name__=="__main__":
    with mp.Pool(8) as p:cs=[c for c in p.map(load2,sorted(glob.glob("/workspace/bt/data/*.json"))) if c]
    cs.sort(key=lambda c:c["born"]);cut=int(len(cs)*0.6);train,hold=cs[:cut],cs[cut:]
    print("coins",len(cs),"train",len(train),"holdout",len(hold),dt.datetime.fromtimestamp(train[0]["born"]).strftime("%H:%M"),dt.datetime.fromtimestamp(hold[0]["born"]).strftime("%H:%M"),dt.datetime.fromtimestamp(hold[-1]["born"]).strftime("%H:%M"))
    W=wallet_stats(train);print("wallets in train",len(W))
    rng=random.Random(3);res=[]
    # search wallet filter + copy genome on TRAIN (wallet stats from train, evaluated on train = in-sample), then report HOLDOUT
    for it in range(300):
        f=(rng.choice([2,3,4,5]),rng.uniform(0.3,0.8),rng.uniform(-0.2,2.0),rng.choice([30,60,150,400]))
        sm=pick(W,*f)
        if len(sm)<5:continue
        g=strat.copy_genome(rng)
        rs=[];[rs.extend(sim_copy(g,c,sm)) for c in train]
        sc=summ(rs)
        if sc["n"]>=30:res.append((sc["mean"]*math.sqrt(sc["n"]),f,g,sc,len(sm)))
    res.sort(key=lambda x:-x[0]);out=[]
    for score,f,g,sc,nw in res[:8]:
        sm=pick(W,*f);rs=[];[rs.extend(sim_copy(g,c,sm)) for c in hold];h=summ(rs)
        print(f"filter coins {f[0]}-{f[3]} win>={f[1]:.2f} pnl>={f[2]:.2f} ({nw} wallets) consensus {round(g['consensus'])} | TRAIN {sc} | HOLDOUT {h}")
        out.append(dict(filter=f,genome=g,train=sc,holdout=h,wallets=sorted(sm)))
    # baseline: copy everyone with >=3 coins
    json.dump(dict(train=len(train),holdout=len(hold),ts=dt.datetime.now().isoformat(),results=out),open("/workspace/sage3/copy_result.json","w"))

"""Shared strategy + paper-fill model. Used identically by the live engine and the backtester.
PAPER ONLY. Prices come from pump.fun bonding-curve state (vSOL, vTokens); fills are simulated on that curve."""
import math,random,copy,collections
K=30*1.073e9                      # pump.fun virtual constant product (SOL * tokens)
FEE=0.0095                        # pump.fun bonding-curve trade fee (~0.95%)
PRIO_SOL=0.0005                   # priority/jito tip per tx in SOL (realistic small-bot cost)
def vs_from_mcap(m):return math.sqrt(max(m,1e-9)*K/1e9)
def mid(c):return c.vsol/c.vtok   # SOL per token
class Coin:
    __slots__=("mint","sym","born","creator","dev_pct","vsol","vtok","mcap","peak","low_since_peak","up","down","inflow","outflow","last_up","dev_sold","early_pct","hist","social_ok","graduated","flag_copy","hard_kill","last_ts")
    def __init__(s,mint,sym,born,creator,dev_pct,mcap=28.0,social_ok=None):
        s.mint=mint;s.sym=sym;s.born=born;s.creator=creator;s.dev_pct=dev_pct;s.mcap=mcap;s.vsol=vs_from_mcap(mcap);s.vtok=K/s.vsol
        s.peak=mcap;s.low_since_peak=mcap;s.up=0;s.down=0;s.inflow=0.0;s.outflow=0.0;s.last_up=born;s.dev_sold=False;s.early_pct=0.0
        s.hist=collections.deque(maxlen=400);s.hist.append((born,mcap));s.social_ok=social_ok;s.graduated=False;s.flag_copy=None;s.hard_kill=None;s.last_ts=born
    def tick(s,ts,vsol,vtok=None):
        """One 1-second observation of the curve (live: RPC poll; backtest: 1s bucket of the trade tape)."""
        vtok=vtok if vtok else K/vsol
        d=vsol-s.vsol
        if d>1e-9:
            s.up+=1;s.inflow+=d;s.last_up=ts
            if ts-s.born<=3:s.early_pct+=100*max(0,s.vtok-vtok)/1e9
        elif d<-1e-9:s.down+=1;s.outflow+=-d
        s.vsol=vsol;s.vtok=vtok;s.mcap=vsol/vtok*1e9;s.last_ts=ts
        if s.mcap>s.peak:s.peak=s.mcap;s.low_since_peak=s.mcap
        s.low_since_peak=min(s.low_since_peak,s.mcap);s.hist.append((ts,s.mcap))
        if s.vsol>=114:s.graduated=True
    def change(s,ts,secs):
        for t,m in reversed(s.hist):
            if ts-t>=secs:return s.mcap/m-1
        return s.mcap/s.hist[0][1]-1
    def age(s,ts):return ts-s.born
# ---------------- paper fills on the curve ----------------
def buy_fill(c,sol):
    """tokens received for `sol` spent (fee + price impact)."""
    net=sol*(1-FEE)-PRIO_SOL
    if net<=0:return 0
    k=c.vsol*c.vtok
    return c.vtok-k/(c.vsol+net)
def sell_fill(c,tokens):
    """SOL received for selling `tokens` (price impact + fee + prio)."""
    if c.graduated:return tokens*(c.vsol/c.vtok)*(1-FEE)*0.97-PRIO_SOL     # after migration assume AMM, 3% worse
    out=c.vsol-c.vsol*c.vtok/(c.vtok+tokens)
    return max(0,out*(1-FEE)-PRIO_SOL)
# ---------------- genomes ----------------
STYLES=["sniper","momentum","dip","copy","migration","sniper_strict"]
RANGES=dict(min_age=(3,90),max_age=(20,900),min_up=(2,30),min_mcap=(29,120),max_mcap=(40,380),min_inflow=(0.5,30),min_ratio=(1.0,4.0),
    min_from_peak=(0.5,0.98),max_avg_up=(0.3,6.0),mom10=(-0.05,0.4),dip_dd=(0.2,0.6),rebound=(0.03,0.3),fresh_s=(2,40),max_early=(5,40),
    stop=(-0.35,-0.04),tp1=(0.1,1.5),tp1_frac=(0.2,0.8),trail=(0.05,0.4),fade_s=(10,180),fade_r=(-0.05,0.15),max_hold=(30,1800),size_pct=(0.03,0.2),social=(0,1),
    consensus=(1,3.49),window_s=(30,900),max_lag_s=(3,60),min_wret=(0,150),max_wtrades=(30,600),follow_sells=(0,1),max_daily=(2,8))
def rand_genome(style=None,rng=random):
    g={k:rng.uniform(*v) for k,v in RANGES.items()};g["style"]=style or rng.choice(STYLES[:3]);g["social"]=round(g["social"]);return g
def mutate(g,rng=random,rate=0.3,scale=0.2):
    g=copy.deepcopy(g)
    for k,(a,b) in RANGES.items():
        if k in g and rng.random()<rate:g[k]=min(b,max(a,g[k]+rng.gauss(0,scale*(b-a))))
    g["social"]=round(g["social"])
    if rng.random()<0.05:g["style"]=rng.choice(STYLES[:3])
    return g
def crossover(a,b,rng=random):
    g={k:(a.get(k,b.get(k,sum(RANGES[k])/2)) if rng.random()<0.5 else b.get(k,a.get(k,sum(RANGES[k])/2))) for k in RANGES};g["style"]=a["style"] if rng.random()<0.5 else b["style"];return g
# ---------------- entry / exit logic (pure) ----------------
def hard_kill(c,ts):
    """Shared kill-first rules (FILTER). Return reason or None. These are never relaxed by evolution."""
    if c.dev_pct>10:return f"dev bought {c.dev_pct:.0f}%"
    if c.early_pct>25:return f"snipers {c.early_pct:.0f}% in 3s"
    if c.dev_sold:return "dev wallet sold"
    if c.hard_kill:return c.hard_kill
    return None
def wants_entry(g,c,ts):
    """Return None to enter, else a short reason (ANALYST view for this bot)."""
    a=c.age(ts)
    if c.mcap<27.5:return "curve drained below launch"
    if a<g["min_age"]:return "too early"
    if c.graduated and g["style"]!="migration":return "graduated"
    if g["social"] and c.social_ok is False:return "no socials"
    if c.early_pct>g["max_early"]:return f"snipers {c.early_pct:.0f}%"
    st=g["style"]
    if st=="copy":return copy_entry(g,c,ts)
    if st=="sniper_strict":
        if c.social_ok is not True:return "no real socials"
        if c.dev_pct>5:return f"dev {c.dev_pct:.0f}% > 5%"
        if c.early_pct>15:return f"snipers {c.early_pct:.0f}% > 15%"
    if st=="migration":
        return None if 300<=c.mcap<=380 and c.change(ts,30)>0 and ts-c.last_up<=g["fresh_s"] else "not near graduation"
    if a>g["max_age"]:return "too old"
    if c.mcap<g["min_mcap"]:return f"mcap {c.mcap:.0f} < {g['min_mcap']:.0f}"
    if c.mcap>g["max_mcap"]:return f"mcap {c.mcap:.0f} > {g['max_mcap']:.0f}"
    if ts-c.last_up>g["fresh_s"]:return "no fresh buys"
    if st=="dip":
        dd=1-c.low_since_peak/c.peak
        if dd<g["dip_dd"]:return "no dip"
        if c.mcap/c.low_since_peak-1<g["rebound"]:return "no rebound yet"
        return None
    if c.up<g["min_up"]:return f"{c.up} buys < {g['min_up']:.0f}"
    if c.inflow<g["min_inflow"]:return f"inflow {c.inflow:.1f} < {g['min_inflow']:.1f}"
    if c.up<g["min_ratio"]*max(c.down,1):return "sell pressure"
    if c.inflow/max(c.up,1)>g["max_avg_up"]:return "whale-driven"
    if c.mcap<g["min_from_peak"]*c.peak:return "dumping from peak"
    if c.change(ts,10)<g["mom10"]:return "momentum stalled"
    return None
def exit_reason(g,c,pos,ts):
    """pos: dict(entry_mcap, peak_mcap, opened, tp_done). Return (reason, fraction) or None."""
    r=c.mcap/pos["entry_mcap"]-1;pos["peak_mcap"]=max(pos["peak_mcap"],c.mcap)
    if c.dev_sold:return ("dev sold",1)
    if r<=g["stop"]:return (f"stop {r:+.0%}",1)
    if pos["tp_done"] and c.mcap<=pos["peak_mcap"]*(1-g["trail"]):return (f"trail {r:+.0%}",1)
    if not pos["tp_done"] and r>=g["tp1"]:return (f"TP +{r:.0%}",g["tp1_frac"])
    held=ts-pos["opened"]
    if held>=g["fade_s"] and r<g["fade_r"] and ts-c.last_up>g["fade_s"]/2:return (f"faded {r:+.0%}",1)
    if held>=g["max_hold"]:return (f"time {r:+.0%}",1)
    if c.graduated and g["style"]!="migration":return (f"graduated {r:+.0%}",1)
    return None

def copy_entry(g,c,ts):
    """Copy-trade: smart wallets (filtered) bought this coin recently; enough distinct wallets agree; we are not too late."""
    sig=[x for x in (c.flag_copy or []) if ts-x[0]<=g["window_s"] and x[2]>=g["min_wret"] and x[3]<=g["max_wtrades"] and x[1] not in g.get("avoid_wallets",[])]
    if not sig:return "no smart-wallet buy"
    w={x[1] for x in sig}
    if len(w)<round(g["consensus"]):return f"{len(w)}/{round(g['consensus'])} wallets agree"
    if ts-max(x[0] for x in sig)>g["max_lag_s"]:return "copy signal too old"
    if c.mcap>g["max_mcap"]:return f"mcap {c.mcap:.0f} > {g['max_mcap']:.0f}"
    if c.mcap<g.get("min_mcap_copy",0):return "mcap below lesson floor"
    if c.graduated:return "graduated"
    return None
def copy_genome(rng=random):
    g=rand_genome("momentum",rng);g.update(style="copy",consensus=rng.choice([1,2,2,2,3]),window_s=rng.uniform(60,600),max_lag_s=rng.uniform(5,40),min_wret=rng.uniform(10,80),
        max_wtrades=rng.uniform(80,500),follow_sells=rng.choice([0,1]),max_mcap=rng.uniform(80,380),size_pct=rng.uniform(0.18,0.32),max_daily=rng.uniform(3,7),
        stop=rng.uniform(-0.35,-0.18),tp1=rng.uniform(0.4,1.2),tp1_frac=rng.uniform(0.3,0.6),trail=rng.uniform(0.15,0.35),fade_s=rng.uniform(120,600),fade_r=rng.uniform(-0.1,0.05),max_hold=rng.uniform(600,3600),avoid_wallets=[])
    return g
def lessons(g,trades):
    """Replacement bots learn from the bot they replace. trades: list of dict(ret, entry_mcap, held_s, exit, wallets)."""
    g=copy.deepcopy(g);notes=[]
    los=[t for t in trades if t["ret"]<=0];win=[t for t in trades if t["ret"]>0]
    if not los:return g,notes
    import statistics as S
    lm=S.median(t["entry_mcap"] for t in los)
    if win:
        wm=S.median(t["entry_mcap"] for t in win)
        if lm>wm*1.15:g["max_mcap"]=min(g["max_mcap"],(lm+wm)/2);notes.append(f"losers entered higher (median {lm:.0f} vs {wm:.0f} SOL mc) → max entry {g['max_mcap']:.0f} SOL")
        elif lm<wm*0.85:g["min_mcap_copy"]=g["min_mcap"]=max(g.get("min_mcap",0),(lm+wm)/2);notes.append(f"losers entered lower (median {lm:.0f} vs {wm:.0f}) → min entry {g['min_mcap']:.0f} SOL")
    elif lm>40:g["max_mcap"]=min(g["max_mcap"],lm*0.8);notes.append(f"all losses; entries around {lm:.0f} SOL mc → max entry {g['max_mcap']:.0f} SOL")
    fast=[t for t in los if t["exit"].startswith("stop") and t["held_s"]<30]
    if len(fast)>=max(2,len(los)/2):
        if g["style"]=="copy":g["consensus"]=min(3.49,g["consensus"]+1);g["max_lag_s"]=max(3,g["max_lag_s"]*0.6);notes.append("most losses were fast stop-outs → needs one more agreeing wallet and a fresher signal")
        else:g["min_age"]=min(RANGES["min_age"][1],g["min_age"]+15);notes.append("most losses were fast stop-outs → waits 15s longer before entering")
    bad=collections.Counter(w for t in los for w in t.get("wallets",[]))
    good=collections.Counter(w for t in win for w in t.get("wallets",[]))
    av=[w for w,n in bad.items() if n>=2 and good[w]==0]
    if av:g["avoid_wallets"]=sorted(set(g.get("avoid_wallets",[]))|set(av));notes.append(f"stops copying {len(av)} wallet(s) that lost twice")
    slow=[t for t in los if t["exit"].startswith(("faded","time"))]
    if len(slow)>=len(los)/2:g["fade_s"]=max(RANGES["fade_s"][0],g["fade_s"]*0.7);notes.append("losses bled out slowly → exits faded coins sooner")
    return g,notes

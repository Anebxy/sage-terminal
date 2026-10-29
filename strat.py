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
STYLES=["sniper","momentum","dip","copy","migration"]
RANGES=dict(min_age=(3,90),max_age=(20,900),min_up=(2,30),min_mcap=(29,120),max_mcap=(40,380),min_inflow=(0.5,30),min_ratio=(1.0,4.0),
    min_from_peak=(0.5,0.98),max_avg_up=(0.3,6.0),mom10=(-0.05,0.4),dip_dd=(0.2,0.6),rebound=(0.03,0.3),fresh_s=(2,40),max_early=(5,40),
    stop=(-0.35,-0.04),tp1=(0.1,1.5),tp1_frac=(0.2,0.8),trail=(0.05,0.4),fade_s=(10,180),fade_r=(-0.05,0.15),max_hold=(30,1800),size_pct=(0.03,0.2),social=(0,1))
def rand_genome(style=None,rng=random):
    g={k:rng.uniform(*v) for k,v in RANGES.items()};g["style"]=style or rng.choice(STYLES[:3]);g["social"]=round(g["social"]);return g
def mutate(g,rng=random,rate=0.3,scale=0.2):
    g=copy.deepcopy(g)
    for k,(a,b) in RANGES.items():
        if rng.random()<rate:g[k]=min(b,max(a,g[k]+rng.gauss(0,scale*(b-a))))
    g["social"]=round(g["social"])
    if rng.random()<0.05:g["style"]=rng.choice(STYLES[:3])
    return g
def crossover(a,b,rng=random):
    g={k:(a[k] if rng.random()<0.5 else b[k]) for k in RANGES};g["style"]=a["style"] if rng.random()<0.5 else b["style"];return g
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
    if st=="copy":
        return None if c.flag_copy and c.mcap<=g["max_mcap"] else "no top-trader buy"
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

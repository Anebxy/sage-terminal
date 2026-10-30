"""Nightly retest on the live recorder (/data/rec/*.jsonl.gz). Chronological 70/30 train/held-out split.
Candidates are ranked on TRAIN only; a config is 'promoted' only if held-out n>=30, held-out mean>0 after modeled fees, and train mean>0."""
import json,glob,gzip,os,sys,random,datetime as dt,math
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)));import bt,strat
from strat import K
DATA=os.environ.get("DATA_DIR","/data");D=os.path.dirname(os.path.abspath(__file__))
def rec_to_coin(r):
    s_=r["series"]
    if len(s_)<5:return None
    coin=strat.Coin(r["mint"],r["sym"],r["born"],r["creator"],r["dev_pct"],28.0,bool(r["social"]));snaps=[]
    for ts,mc in s_:
        vs=strat.vs_from_mcap(mc);coin.tick(ts,vs,K/vs)
        if r.get("dev_sold_ts") and ts>=r["dev_sold_ts"]:coin.dev_sold=True
        s=bt.Snap()
        for a in ("mcap","vsol","vtok","up","down","inflow","last_up","peak","low_since_peak","early_pct","dev_sold","graduated","born","dev_pct","social_ok","flag_copy","hard_kill"):setattr(s,a,getattr(coin,a))
        s.early_pct=max(s.early_pct,r.get("early_pct",0));s.ts=ts;s.ch10=coin.change(ts,10);s.ch30=coin.change(ts,30);snaps.append(s)
    return {"mint":r["mint"],"sym":r["sym"],"born":r["born"],"trunc":False,"snaps":snaps}
def load_all():
    out=[]
    for f in sorted(glob.glob(f"{DATA}/rec/*.jsonl.gz")):
        try:
            with gzip.open(f,"rt") as fh:
                for line in fh:
                    try:
                        c=rec_to_coin(json.loads(line))
                        if c:out.append(c)
                    except Exception:pass
        except Exception:pass   # partially written member at the tail
    out.sort(key=lambda c:c["born"]);return out
def main():
    cs=load_all();res=dict(ts=dt.datetime.now().isoformat(timespec="seconds"),coins=len(cs))
    if len(cs)<200:res.update(status=f"not enough recorded coins yet ({len(cs)})");json.dump(res,open(f"{DATA}/retest.json","w"));return
    span=(cs[-1]["born"]-cs[0]["born"])/86400;cut=int(len(cs)*0.7);tr,ho=cs[:cut],cs[cut:]
    f=lambda t:dt.datetime.fromtimestamp(t).strftime("%b %d %H:%M")
    res.update(span_days=round(span,2),train=[f(tr[0]["born"]),f(tr[-1]["born"]),len(tr)],hold=[f(ho[0]["born"]),f(ho[-1]["born"]),len(ho)])
    base=[x["genome"] for x in json.load(open(f"{D}/bt_result.json"))["bots"] if x["genome"]["style"] in("sniper","momentum")]
    live=json.loads(sys.argv[1]) if len(sys.argv)>1 else []
    rng=random.Random(int(dt.date.today().strftime("%Y%m%d")));cand=[dict(g,src="v10 backtest") for g in base]+[dict(g,src="live bot") for g in live]
    for _ in range(40):cand.append(dict(strat.mutate(rng.choice(base),rng),src="mutation"))
    scored=[(bt.evaluate(g,tr),g) for g in cand];scored.sort(key=lambda x:-x[0]["score"])
    rows=[]
    for a,g in scored[:12]:
        b=bt.evaluate(g,ho);ok=b["n"]>=30 and b["mean"]>0 and a["mean"]>0
        rows.append(dict(genome=g,src=g.get("src"),style=g["style"],train=dict(n=a["n"],mean=round(a["mean"],4),win=round(a["win"],3)),holdout=dict(n=b["n"],mean=round(b["mean"],4),win=round(b["win"],3)),promoted=ok))
    res.update(status="ok",rows=rows,promoted=sum(r["promoted"] for r in rows),eligible=span>=2.0)
    json.dump(res,open(f"{DATA}/retest.json","w"))
    with open(f"{DATA}/retest_log.jsonl","a") as fh:fh.write(json.dumps({k:v for k,v in res.items() if k!="rows"}|{"top":[(r["style"],r["src"],r["train"],r["holdout"],r["promoted"]) for r in rows[:5]]})+"\n")
if __name__=="__main__":main()

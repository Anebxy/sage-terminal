"""Exit-rule test: same 4 live genomes, uniform exit variants. Pick by TRAIN, confirm on HOLDOUT (chronological 70/30)."""
import json,glob,os,sys,pickle,multiprocessing as mp,itertools
sys.path.insert(0,'/workspace/sage3');import bt,strat
def variants(g):
    out={}
    for tf,tr,fd in itertools.product([None,0.25],[None,1.5,2.0],[None,"loose"]):
        h=dict(g);name=[]
        if tf:h["tp1_frac"]=min(g["tp1_frac"],tf);name.append(f"tp1frac{tf}")
        if tr:h["trail"]=min(0.4,g["trail"]*tr);name.append(f"trail×{tr}")
        if fd:h["fade_s"]=min(180,g["fade_s"]*2);h["fade_r"]=g["fade_r"]-0.1;name.append("fade loose")
        out["+".join(name) or "current"]=h
    return out
_C=None
def _init(p):
    global _C;_C=pickle.load(open(p,"rb"))
def _ev(g):return bt.evaluate(g,_C)
if __name__=="__main__":
    files=sorted(glob.glob("/workspace/bt/data/*.json"))
    with mp.Pool(os.cpu_count()) as p:cs=[c for c in p.map(bt.load,files) if c and not c["trunc"]]
    cs+=[c for c in (json.load(open(sys.argv[1])) if len(sys.argv)>1 else [])]
    cs.sort(key=lambda c:c["born"]);cut=int(len(cs)*0.7);tr,ho=cs[:cut],cs[cut:]
    pickle.dump(tr,open("/tmp/xtr.pkl","wb"));pickle.dump(ho,open("/tmp/xho.pkl","wb"))
    import datetime as dt;f=lambda t:dt.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")
    print(f"tapes {len(cs)} train {len(tr)} {f(tr[0]['born'])}..{f(tr[-1]['born'])} holdout {len(ho)} ..{f(ho[-1]['born'])}",flush=True)
    G=[x["genome"] for x in json.load(open("/workspace/paper-terminal-deploy/bt_result.json"))["bots"] if x["genome"]["style"] in("sniper","momentum") and x["holdout"]["n"]>=25 and x["holdout"]["mean"]>0 and x["train"]["mean"]>0]
    V=[variants(g) for g in G];names=list(V[0]);jobs=[V[i][n] for n in names for i in range(len(G))]
    res={}
    for tag,pk in(("train","/tmp/xtr.pkl"),("hold","/tmp/xho.pkl")):
        with mp.Pool(os.cpu_count(),_init,(pk,)) as p:r=p.map(_ev,jobs)
        k=0
        for n in names:
            rs=r[k:k+len(G)];k+=len(G);N=sum(x["n"] for x in rs);S=sum(x["sum"] for x in rs)
            res.setdefault(n,{})[tag]=dict(n=N,mean=S/max(N,1),win=sum(x["win"]*x["n"] for x in rs)/max(N,1),per_bot=[(x["n"],round(x["mean"],4)) for x in rs])
    for n in sorted(names,key=lambda n:-res[n]["train"]["mean"]):
        a,b=res[n]["train"],res[n]["hold"];print(f"{n:35s} TRAIN n={a['n']:5d} mean {a['mean']:+.4f} win {a['win']:.0%} | HOLD n={b['n']:5d} mean {b['mean']:+.4f} win {b['win']:.0%}")
    json.dump(res,open("/workspace/sage3/exit_result.json","w"),indent=1)

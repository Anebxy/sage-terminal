"""Harvest real pump.fun launches + their trade tapes (GeckoTerminal public trades, <=300 most recent; OHLCV fallback if truncated).
Read-only, free, no keys. Runs continuously."""
import json,time,os,urllib.request,random
D="/workspace/bt";DD=f"{D}/data";os.makedirs(DD,exist_ok=True)
CAND=f"{D}/coins.jsonl";seen=set()
if os.path.exists(CAND):
    for l in open(CAND):seen.add(json.loads(l)["mint"])
def get(u,t=20):
    for i in range(3):
        try:return json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=t))
        except urllib.error.HTTPError as e:
            if e.code==429:time.sleep(20);continue
            if e.code==404:return None
            time.sleep(3)
        except Exception:time.sleep(3)
    return None
last_g=[0]
def gecko(u):
    w=last_g[0]+2.1-time.time()
    if w>0:time.sleep(w)
    last_g[0]=time.time();return get(u)
last_list=0
while True:
    if time.time()-last_list>900:
        n=0
        for off in range(0,1500,50):
            d=get(f"https://frontend-api-v3.pump.fun/coins?offset={off}&limit=50&sort=created_timestamp&order=DESC&includeNsfw=false") or []
            with open(CAND,"a") as f:
                for c in d:
                    if c.get("mint") in seen or not c.get("bonding_curve"):continue
                    seen.add(c["mint"]);n+=1
                    f.write(json.dumps({k:c.get(k) for k in ("mint","symbol","name","creator","bonding_curve","created_timestamp","twitter","website","telegram","ath_market_cap","usd_market_cap","complete","quote_mint")})+"\n")
            time.sleep(0.4)
        last_list=time.time();print(time.strftime("%T"),"listed",n,"new coins; total",len(seen),flush=True)
    # fetch tapes for coins aged >=60 min, not yet fetched; prioritize ones that moved
    todo=[]
    for l in open(CAND):
        c=json.loads(l)
        if os.path.exists(f"{DD}/{c['mint']}.json"):continue
        if time.time()*1000-c["created_timestamp"]<1500e3:continue
        todo.append(c)
    random.shuffle(todo)
    if not todo:time.sleep(30);continue
    for c in todo[:60]:
        out={"coin":c}
        if (c.get("ath_market_cap") or 0)<30 and (c.get("quote_mint") in (None,"11111111111111111111111111111111")):   # ath in SOL? (field is quote units) -> never moved
            pass
        t=gecko(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{c['bonding_curve']}/trades")
        out["trades"]=[x["attributes"] for x in (t or {}).get("data",[])]
        if len(out["trades"])>=300:
            o=gecko(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{c['bonding_curve']}/ohlcv/minute?aggregate=1&limit=1000&currency=usd")
            out["ohlcv"]=((o or {}).get("data") or {}).get("attributes",{}).get("ohlcv_list")
        json.dump(out,open(f"{DD}/{c['mint']}.json","w"))
    print(time.strftime("%T"),"tapes",len(os.listdir(DD)),flush=True)

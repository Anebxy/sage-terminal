import json,itertools,sys
U=["BTC","ETH","SOL","BNB","XRP","DOGE","ZEC","NEAR","SUI","LINK"]
FEE=0.001;SLIP=0.0005
def ema(a,n):
    k=2/(n+1);o=[];e=a[0]
    for x in a: e=x*k+e*(1-k);o.append(e)
    return o
def ind(b):
    c=[x[4] for x in b];h=[x[2] for x in b];l=[x[3] for x in b]
    tr=[h[0]-l[0]]+[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(b))]
    atr=ema(tr,14)
    g=[0]+[max(c[i]-c[i-1],0) for i in range(1,len(c))];d=[0]+[max(c[i-1]-c[i],0) for i in range(1,len(c))]
    ag=ema(g,14);ad=ema(d,14);rsi=[100-100/(1+ag[i]/ad[i]) if ad[i]>0 else 100 for i in range(len(c))]
    # 4h: ema over last-completed 4h closes, mapped to hourly index
    t4=[];idx=[]
    for i,x in enumerate(b):
        if (x[0]//3600)%4==3: t4.append(c[i]);idx.append(i)
    e4f=ema(t4,20);e4s=ema(t4,50);up=[False]*len(b);j=-1
    for i in range(len(b)):
        while j+1<len(idx) and idx[j+1]<=i-0: j+=1  # completed at close of bar idx
        up[i]= j>=50 and t4[j]>e4s[j] and e4f[j]>e4s[j]
    return dict(c=c,h=h,l=l,o=[x[1] for x in b],atr=atr,rsi=rsi,up=up,e20=ema(c,20),t=[x[0] for x in b])
def signal(cfg,I,i):
    if not I['up'][i]: return False
    if cfg['s']=='trend':
        n=cfg['n'];return i>n and I['c'][i]>max(I['h'][i-n:i])
    else:
        return I['rsi'][i]<cfg['rsi'] and I['c'][i]>I['c'][i-1]  # bounce candle after oversold
def sim(cfg,D,t0,t1,risk=0.015,maxpos=4,cap=0.35):
    eq=1000.0;pos={};trades=[];curve=[];peak=eq;mdd=0;last={}
    T=sorted(set(t for k in D for t in D[k]['t'] if t0<=t<t1))
    tix={k:{t:i for i,t in enumerate(D[k]['t'])} for k in D}
    for t in T:
        for k in list(pos):
            I=D[k];i=tix[k].get(t)
            if i is None: continue
            p=pos[k];px=None;why=None
            if I['l'][i]<=p['stop']: px=min(p['stop'],I['o'][i])*(1-SLIP);why='stop'
            elif p.get('tp') and I['h'][i]>=p['tp']: px=p['tp']*(1-SLIP);why='target'
            elif i-p['i']>=cfg['hold']: px=I['c'][i]*(1-SLIP);why='time'
            elif cfg['s']=='trend': p['stop']=max(p['stop'],I['c'][i]-cfg['trail']*I['atr'][i])
            if px:
                pnl=p['q']*px*(1-FEE)-p['cost'];eq+=pnl;trades.append(dict(k=k,pnl=pnl,r=pnl/p['risk'],why=why,t=t));del pos[k]
        for k in D:
            if k in pos or len(pos)>=maxpos: continue
            I=D[k];i=tix[k].get(t)
            if i is None or i<300 or last.get(k,-99)>i-cfg.get('cool',6): continue
            if signal(cfg,I,i):
                e=I['c'][i]*(1+SLIP);st=e-cfg['k']*I['atr'][i]
                if st<=0 or (e-st)/e<0.004: continue
                q=min(risk*eq/(e-st),cap*eq/e);cost=q*e*(1+FEE)
                pos[k]=dict(q=q,cost=cost,stop=st,i=i,risk=risk*eq,tp=(e+cfg['tp']*(e-st)) if cfg.get('tp') else None);last[k]=i
        mtm=eq+sum(p['q']*D[k]['c'][tix[k].get(t,p['i'])]-p['cost'] for k,p in pos.items())
        peak=max(peak,mtm);mdd=max(mdd,1-mtm/peak);curve.append((t,mtm))
    w=[x for x in trades if x['pnl']>0]
    days=(t1-t0)/86400
    return dict(n=len(trades),wr=len(w)/max(1,len(trades)),net=eq-1000,mdd=mdd,tpd=len(trades)/days,trades=trades,curve=curve)
def grid():
    G=[]
    for n,k,tr,h in itertools.product([24,48],[1.5,2.5],[2,3,4],[48,120]): G.append(dict(s='trend',n=n,k=k,trail=tr,hold=h))
    for r,k,tp,h in itertools.product([25,30,35],[1.5,2.5],[1.5,2.5],[24,72]): G.append(dict(s='pull',rsi=r,k=k,tp=tp,hold=h))
    return G
if __name__=='__main__':
    D={k:ind(json.load(open(k+'.json'))) for k in U}
    t0=max(D[k]['t'][0] for k in D)+300*3600;t1=min(D[k]['t'][-1] for k in D);ts=t0+0.7*(t1-t0)
    print('train',t0,ts,'hold',ts,t1)
    R=[]
    for g in grid():
        a=sim(g,D,t0,ts);b=sim(g,D,ts,t1)
        R.append(dict(cfg=g,train={x:a[x] for x in('n','wr','net','mdd','tpd')},hold={x:b[x] for x in('n','wr','net','mdd','tpd')}))
        print(g,{x:round(a[x],3) for x in('n','wr','net','mdd','tpd')},'|',{x:round(b[x],3) for x in('n','wr','net','mdd','tpd')},flush=True)
    sol=D['SOL'];
    def px(t):
        return sol['c'][min(range(len(sol['t'])),key=lambda i:abs(sol['t'][i]-t))]
    bh=dict(train=px(ts)/px(t0)-1,hold=px(t1)/px(ts)-1)
    json.dump(dict(t0=t0,ts=ts,t1=t1,solbh=bh,R=R),open('cres.json','w'));print('SOL B&H',bh)

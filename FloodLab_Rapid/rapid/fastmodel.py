"""Screening model, not a time-stepping hydraulic solver. Depths are scenario estimates."""
import heapq
import numpy as np
from scipy.ndimage import label


def drainage(z, valid):
    """Priority flood from open domain edges; D8 depression filling, parent DAG."""
    ny,nx=z.shape; a=z.ravel(); ok=valid.ravel(); N=a.size
    filled=np.full(N,np.nan); parent=np.full(N,-1,np.int32); seen=~ok.copy(); heap=[]; order=[]
    for i in np.flatnonzero(ok):
        y,x=divmod(int(i),nx)
        if y==0 or x==0 or y==ny-1 or x==nx-1 or not valid[y-1:y+2,x-1:x+2].all():
            seen[i]=True; filled[i]=a[i]; heapq.heappush(heap,(float(a[i]),int(i)))
    while heap:
        h,i=heapq.heappop(heap); order.append(i); y,x=divmod(i,nx)
        for dy,dx in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
            yy,xx=y+dy,x+dx
            if 0<=yy<ny and 0<=xx<nx:
                k=yy*nx+xx
                if seen[k]: continue
                seen[k]=True; parent[k]=i; v=max(h,float(a[k])); filled[k]=v; heapq.heappush(heap,(v,k))
    return filled.reshape(z.shape), parent, np.asarray(order,np.int32)


def ponding(z,valid,rain_m,runoff_fraction,dx=30,prepared=None):
    """Route cumulative runoff to first depressions; fill to spill elevation; route excess.
    No time/dynamic flow, infiltration represented by scenario runoff fraction.
    """
    filled,parent,order=prepared if prepared is not None else drainage(z,valid)
    depression=valid & (filled-z>1e-5)
    # Split basins at differing spill levels (adjacent depressions with unequal spill
    # elevation must not share a storage reservoir). Priority-flood parent assigns basins.
    N=z.size; flat=z.ravel(); fl=filled.ravel(); wet=depression.ravel(); groups=np.zeros(N,np.int32)
    exits=[-1]; level=[0.]; count=0
    for i in order:
        if wet[i]:
            p=parent[i]
            if p>=0 and groups[p]>0 and abs(fl[p]-fl[i])<1e-7:
                groups[i]=groups[p]
            else:
                count+=1; groups[i]=count; exits.append(int(p)); level.append(float(fl[i]))
    # Merge adjacent equal-spill depressions where flood seeds touch, without allowing
    # cross-level storage. Union-find on group IDs; earliest ID owns spill path.
    root=np.arange(count+1,dtype=np.int32)
    def find(a):
        while root[a]!=a:
            root[a]=root[root[a]]; a=int(root[a])
        return a
    g=groups.reshape(z.shape)
    for aa,bb in ((g[:,:-1],g[:,1:]),(g[:-1,:],g[1:,:]),(g[:-1,:-1],g[1:,1:]),(g[1:,:-1],g[:-1,1:])):
        m=(aa>0)&(bb>0)&(aa!=bb)
        for a,b in np.unique(np.stack((aa[m],bb[m]),axis=1),axis=0):
            if abs(level[a]-level[b])<1e-7:
                x,y=find(int(a)),find(int(b)); root[max(x,y)]=min(x,y)
    roots=np.array([find(i) for i in range(count+1)],np.int32); groups=roots[groups]
    area=dx*dx; runoff=np.where(valid,np.maximum(0,np.asarray(rain_m)*np.asarray(runoff_fraction)),0).ravel()*area
    supplied=float(runoff.sum()); dst=np.zeros(N,np.int32)
    for i in order:
        dst[i]=groups[i] if groups[i] else (dst[parent[i]] if parent[i]>=0 else 0)
    storage=np.bincount(dst,weights=runoff,minlength=count+1); exported=float(storage[0]); depth=np.zeros(N,float)
    ids=np.flatnonzero(groups); ids=ids[np.argsort(groups[ids],kind='stable')]; unique,starts=np.unique(groups[ids],return_index=True)
    lookup={int(k):ids[a:b] for k,a,b in zip(unique,starts,np.r_[starts[1:],len(ids)])}
    # Earliest seed of each merged group has an exit already visited before that group.
    for k in sorted(lookup,reverse=True):
        cells=lookup[k]; heights=flat[cells]; cap=float(np.sum(level[k]-heights)*area); v=min(float(storage[k]),cap)
        if v>0:
            lo=float(heights.min()); hi=level[k]
            for _ in range(28):
                mid=(lo+hi)/2
                if np.maximum(mid-heights,0).sum()*area<v: lo=mid
                else: hi=mid
            depth[cells]=np.maximum((lo+hi)/2-heights,0)
        extra=max(0.,float(storage[k])-v); p=exits[k]; downstream=int(dst[p]) if p>=0 else 0
        if 0<downstream<k: storage[downstream]+=extra
        else: exported+=extra
    depth=depth.reshape(z.shape); depth[~valid]=np.nan; retained=float(np.nansum(depth)*area)
    return depth,dict(runoff_m3=supplied,ponded_m3=retained,exported_m3=exported,residual_m3=supplied-retained-exported,depressions=len(lookup))


def runoff_scenarios(classes, moisture):
    # Screening coefficients, not calibrated CN or measured infiltration.
    # Moisture proxy from volumetric soil water; uncertainty intentionally broad.
    m=np.where(np.isfinite(moisture),np.clip((moisture-.12)/.30,0,1),.5)
    center=.20+.50*m
    if classes is not None:
        center=np.where(classes==50,.85,center)
        center=np.where(np.isin(classes,[10,20]),center*.75,center)
        center=np.where(classes==80,1.,center)
    return [np.clip(center-.25,0,1),center,np.clip(center+.25,0,1)]


def metrics(depth,reference,valid,threshold=.05):
    ok=valid & np.isfinite(reference)&np.isfinite(depth); p=depth>=threshold; t=reference==1
    tp=int((ok&p&t).sum()); fp=int((ok&p&~t).sum()); fn=int((ok&~p&t).sum()); tn=int((ok&~p&~t).sum()); div=lambda a,b:a/b if b else None
    return dict(tp=tp,fp=fp,fn=fn,tn=tn,iou=div(tp,tp+fp+fn),f1=div(2*tp,2*tp+fp+fn),precision=div(tp,tp+fp),recall=div(tp,tp+fn),predicted_km2=(tp+fp)*.0009,observed_km2=(tp+fn)*.0009,coverage_km2=int(ok.sum())*.0009)

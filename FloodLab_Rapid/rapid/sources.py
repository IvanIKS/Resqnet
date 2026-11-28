import os, time, json, hashlib, calendar, math
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import requests, rasterio as rio
from rasterio.warp import transform_bounds,reproject,Resampling
from rasterio.windows import from_bounds, Window
from rasterio.transform import array_bounds
from pyproj import Transformer
from .geo import remote

ROOT=Path(__file__).resolve().parents[1]; CACHE=ROOT/'.cache'; JRC='https://digital-atlas.s3.amazonaws.com/cdh/data/jrc-glofas-v2.1.2/'


def json_request(url,p,ttl):
    # API credentials never written to cache metadata or returned in errors.
    key=os.getenv('OPEN_METEO_API_KEY')
    if key and 'open-meteo.com' in url:
        url=url.replace('https://','https://customer-',1); p=dict(p,apikey=key)
    token=hashlib.sha256(json.dumps([url,p],sort_keys=True).encode()).hexdigest(); d=CACHE/'json'; d.mkdir(parents=True,exist_ok=True); path=d/(token+'.json')
    if path.exists() and time.time()-path.stat().st_mtime<ttl:
        try: return json.loads(path.read_text())
        except Exception: pass
    try:
        r=requests.get(url,params=p,timeout=(5,20)); r.raise_for_status(); j=r.json()
        if isinstance(j,dict) and j.get('error'): raise ValueError('provider returned error')
    except Exception as e: raise RuntimeError(f'{url.split("/")[2]}: {type(e).__name__} (network, quota, date coverage or access)') from None
    tmp=path.with_suffix(f'.{os.getpid()}.tmp'); tmp.write_text(json.dumps(j)); os.replace(tmp,path); return j


def cached_raster(grid,kind,loader):
    key=hashlib.sha256(json.dumps([kind,grid.bbox,grid.shape,str(grid.crs),tuple(grid.transform)]).encode()).hexdigest(); d=CACHE/'raster'; d.mkdir(parents=True,exist_ok=True); path=d/(key+'.npz')
    if path.exists() and time.time()-path.stat().st_mtime<7*86400:
        try:
            with np.load(path,allow_pickle=False) as f: return f['a'],json.loads(str(f['meta']))
        except Exception: pass
    a,meta=loader()
    if np.isfinite(a).any() and not meta.get('errors'):
        tmp=path.with_suffix(f'.{os.getpid()}.tmp.npz'); np.savez_compressed(tmp,a=a,meta=json.dumps(meta)); os.replace(tmp,path)
    return a,meta


def terrain(grid,kind):
    def load():
        a,urls,errors=remote(grid,kind); return a,dict(urls=urls,errors=errors,coverage=float(np.isfinite(a).mean()))
    return cached_raster(grid,kind,load)


def jrc_map(grid,rp):
    if rp not in (10,20,50,75,100,200,500,'spurious-depths','permanent-water'): raise ValueError('invalid map')
    name=f'depth-rp{rp}' if isinstance(rp,int) else rp; url=JRC+name+'.vrt'
    def load():
        with rio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR',GDAL_HTTP_TIMEOUT='12',GDAL_HTTP_CONNECTTIMEOUT='5',GDAL_HTTP_MAX_RETRY='0'):
            with rio.open(url) as src:
                b=transform_bounds(grid.crs,src.crs,*array_bounds(*grid.shape,grid.transform),densify_pts=21)
                w=from_bounds(*b,transform=src.transform); x=math.floor(w.col_off)-2; y=math.floor(w.row_off)-2; win=Window(x,y,math.ceil(w.col_off+w.width)-x+2,math.ceil(w.row_off+w.height)-y+2)
                win=win.intersection(Window(0,0,src.width,src.height)); a=src.read(1,window=win,masked=True).astype('float32').filled(np.nan)
                out=np.full(grid.shape,np.nan,np.float32)
                reproject(a,out,src_transform=src.window_transform(win),src_crs=src.crs,src_nodata=np.nan,dst_transform=grid.transform,dst_crs=grid.crs,dst_nodata=np.nan,resampling=Resampling.nearest)
        return out,dict(url=url,coverage=float(np.isfinite(out).mean()),native_resolution_m=90)
    return cached_raster(grid,'jrc-'+name,load)


def sample_points(grid):
    w,s,e,n=grid.bbox; return [((s+n)/2,(w+e)/2),(s+.2*(n-s),w+.2*(e-w)),(s+.2*(n-s),e-.2*(e-w)),(n-.2*(n-s),w+.2*(e-w)),(n-.2*(n-s),e-.2*(e-w))]


def point_params(points): return dict(latitude=','.join(f'{y:.5f}' for y,x in points),longitude=','.join(f'{x:.5f}' for y,x in points),timezone='GMT')

def as_list(j): return j if isinstance(j,list) else [j]

def parse_time(s):
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def weather_totals(j,start,end,moisture_key):
    h=j['hourly']; units=j.get('hourly_units',{})
    if units.get('precipitation')!='mm': raise ValueError('Unexpected rain units')
    total=0.; covered=0.; antecedent=0.; ante_cov=0.; moisture=np.nan; mt=None; ante_start=start-timedelta(hours=72)
    for i,(t,v) in enumerate(zip(h['time'],h['precipitation'])):
        b=parse_time(t); a=b-timedelta(hours=1)
        overlap=max(0.,(min(end,b)-max(start,a)).total_seconds()); pre=max(0.,(min(start,b)-max(ante_start,a)).total_seconds())
        if v is not None and np.isfinite(v) and v>=0:
            total+=float(v)*overlap/3600; covered+=overlap; antecedent+=float(v)*pre/3600; ante_cov+=pre
        vals=h.get(moisture_key,[])
        if b<=start and i<len(vals) and vals[i] is not None and np.isfinite(vals[i]) and 0<=vals[i]<=1 and (mt is None or b>mt): moisture=float(vals[i]); mt=b
    if abs(covered-(end-start).total_seconds())>1: raise ValueError('Incomplete precipitation series')
    if mt is not None and (start-mt).total_seconds()>6*3600: moisture=np.nan
    return dict(lat=float(j['latitude']),lon=float(j['longitude']),rain_mm=total,antecedent_mm=antecedent if ante_cov>=72*3600-1 else None,moisture=moisture if np.isfinite(moisture) else None)


def weather(grid,start,end,historical):
    points=sample_points(grid); p=point_params(points); key='soil_moisture_0_to_7cm' if historical else 'soil_moisture_3_to_9cm'
    p.update(start_date=(start-timedelta(hours=72)).date().isoformat(),end_date=(end+timedelta(hours=1)).date().isoformat(),hourly='precipitation,'+key)
    url='https://archive-api.open-meteo.com/v1/archive' if historical else 'https://api.open-meteo.com/v1/forecast'
    if historical: p['models']='era5' # ERA5 provides both precipitation and soil water; 25 km native grid
    try: j=json_request(url,p,30*86400 if historical else 900)
    except RuntimeError:
        # Missing soil field must not prevent rain; fallback is explicit.
        p['hourly']='precipitation'; j=json_request(url,p,30*86400 if historical else 900)
    out=[]
    for item in as_list(j):
        try: out.append(weather_totals(item,start,end,key))
        except (KeyError,ValueError,TypeError): continue
    if not out: raise ValueError('No complete hourly precipitation series')
    # Deduplicate native cells; nearest neighbour, no invented meteorological resolution.
    out=list({(p['lat'],p['lon']):p for p in out}.values()); xx,yy=coordinates(grid); tr=Transformer.from_crs(4326,grid.crs,always_xy=True); best=np.full(grid.shape,np.inf); rain=np.zeros(grid.shape); moist=np.full(grid.shape,np.nan)
    for p in out:
        x,y=tr.transform(p['lon'],p['lat']); d=(xx-x)**2+(yy-y)**2; m=d<best; best[m]=d[m]; rain[m]=p['rain_mm']/1000; moist[m]=p['moisture'] if p['moisture'] is not None else np.nan
    return rain,moist,dict(provider='Open-Meteo ERA5' if historical else 'Open-Meteo forecast (best match)',points=out,requested_points=points,method='nearest native weather cell; preceding-hour sums; no 30 m weather detail',partial=len(as_list(j))!=len(points) or len(out)<len({(x.get('latitude'),x.get('longitude')) for x in as_list(j)}))


def coordinates(grid):
    y,x=np.indices(grid.shape); return grid.transform.c+(x+.5)*30,grid.transform.f-(y+.5)*30


def annual_maxima(j,first,last):
    if j.get('daily_units',{}).get('river_discharge') not in ('m³/s','m3/s'): raise ValueError('unexpected GloFAS units')
    vals={}
    for t,v in zip(j['daily']['time'],j['daily']['river_discharge']):
        year=int(t[:4]);
        if first<=year<=last and v is not None and np.isfinite(v) and v>=0: vals.setdefault(year,{})[t]=v
    return np.array([max(a.values()) for yr,a in sorted(vals.items()) if len(a)==365+calendar.isleap(yr)],float)


def empirical_rp(annual,q):
    a=np.sort(annual); n=len(a)
    if n<15 or a[-1]<=0 or not np.isfinite(q) or q<0: return None
    if q>a[-1]: return float(n+1) # censored lower bound, not extrapolation
    if q<a[0]: return 1.
    # Duplicate quantiles assigned conservative smaller rank.
    x,idx=np.unique(a,return_index=True); r=(n+1)/(n-idx)
    return float(np.interp(q,x,r))


def glofas(grid,start,end,points=None):
    pts=points or sample_points(grid); p=point_params(pts); p.update(daily='river_discharge',cell_selection='nearest'); url='https://flood-api.open-meteo.com/v1/flood'
    last=min(2021,start.year-1); first=max(1984,last-29)
    if last-first+1<15: raise ValueError('Not enough pre-event years for empirical baseline (choose >=1999).')
    with ThreadPoolExecutor(max_workers=2) as pool:
        event=pool.submit(json_request,url,dict(p,start_date=start.date().isoformat(),end_date=(end-timedelta(microseconds=1)).date().isoformat()),900)
        hist=pool.submit(json_request,url,dict(p,start_date=f'{first}-01-01',end_date=f'{last}-12-31'),30*86400)
        ej,hj=as_list(event.result()),as_list(hist.result())
    h={(round(float(v['latitude']),5),round(float(v['longitude']),5)):v for v in hj}; out=[]; seen=set()
    for j in ej:
        k=(round(float(j['latitude']),5),round(float(j['longitude']),5))
        if k in seen: continue
        seen.add(k); b=h.get(k)
        if b is None: continue
        if j.get('daily_units',{}).get('river_discharge') not in ('m³/s','m3/s'): continue
        t=j.get('daily',{}).get('time',[]); q=j.get('daily',{}).get('river_discharge',[])
        expected=(end-timedelta(microseconds=1)).date()-start.date()
        if len(t)!=expected.days+1 or len(q)!=len(t) or any(v is None or not np.isfinite(v) or v<0 for v in q): continue
        annual=annual_maxima(b,first,last); peak=float(max(q)); rp=empirical_rp(annual,peak)
        if rp is not None: out.append(dict(lat=k[0],lon=k[1],peak_daily_m3s=peak,dates=t,discharge_m3s=q,annual_max_years=len(annual),historical_max_m3s=float(annual.max()),rp_empirical=rp,censored=peak>annual.max()))
    if not out: raise ValueError('No matched GloFAS cells with complete event and >=15 baseline years')
    return dict(provider='GloFAS v4 via Open-Meteo',points=out,requested_points=pts,baseline_years=[first,last],method='Empirical annual maxima plotting positions; daily peak is not local overbank discharge; river identity unverified',regional_rp=max(x['rp_empirical'] for x in out),partial=len(out)<len(seen))


def bracket(rp):
    levels=[10,20,50,75,100,200,500]
    if rp<10: return None
    hi=next((x for x in levels if x>=rp),levels[-1]); lo=max(x for x in levels if x<=min(rp,levels[-1])); return lo,hi



def river_network(grid):
    """Current OSM river/stream geometries; not a historical channel survey."""
    from rasterio.features import rasterize
    from rasterio.warp import transform_geom
    w,s,e,n=grid.bbox
    query=f'[out:json][timeout:12];way["waterway"~"^(river|stream)$"]({s},{w},{n},{e});out tags geom;'
    j=json_request('https://overpass-api.de/api/interpreter',{'data':query},7*86400)
    if j.get('remark'): raise ValueError('Overpass returned incomplete result')
    geoms=[]; groups={}; cy,cx=(s+n)/2,(w+e)/2
    for a in j.get('elements',[]):
        xy=[(float(v['lon']),float(v['lat'])) for v in a.get('geometry',[]) if 'lat' in v and 'lon' in v]
        if len(xy)<2:continue
        tags=a.get('tags',{}); geoms.append(transform_geom('EPSG:4326',grid.crs,{'type':'LineString','coordinates':xy}))
        if tags.get('waterway')!='river':continue
        inside=[(y,x) for x,y in xy if w<=x<=e and s<=y<=n]
        if not inside:continue
        name=tags.get('name') or f"OSM way {a.get('id')}"
        groups.setdefault(name,[]).extend(inside)
    candidates=[]
    for name,points in sorted(groups.items(),key=lambda kv:-len(kv[1])):
        y,x=min(points,key=lambda p:(p[0]-cy)**2+(p[1]-cx)**2)
        if all((y-a['lat'])**2+(x-a['lon'])**2>.000025 for a in candidates):candidates.append(dict(name=name,lat=y,lon=x))
        if len(candidates)==5:break
    # Fill unused slots with geographically separated spatial samples, not invented rivers.
    pts=[(p['lat'],p['lon']) for p in candidates]
    for pt in sample_points(grid):
        if len(pts)>=5:break
        if all((pt[0]-y)**2+(pt[1]-x)**2>.0001 for y,x in pts):pts.append(pt)
    if not pts:pts=sample_points(grid)
    a=rasterize(geoms,out_shape=grid.shape,transform=grid.transform,all_touched=True,dtype='uint8') if geoms else np.zeros(grid.shape,np.uint8)
    return a.astype(float),pts,dict(status='ok',provider='OpenStreetMap via public Overpass (current geometry)',ways=len(geoms),river_candidates=candidates,query_points=pts,license='ODbL; © OpenStreetMap contributors')


def river_bundle(grid,start,end):
    try:a,pts,meta=river_network(grid)
    except Exception as ex:a=None;pts=None;meta=dict(status='unavailable',reason=str(ex)[:200],fallback='spatial samples, river identity unknown')
    try:return glofas(grid,start,end,pts),a,meta,None
    except Exception as ex:return None,a,meta,str(ex)[:200]

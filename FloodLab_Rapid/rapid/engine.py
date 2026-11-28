import json, time, math
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import numpy as np, rasterio as rio
from rasterio.warp import reproject,Resampling,calculate_default_transform,transform_bounds
from rasterio.transform import array_bounds
from pyproj import Geod
from PIL import Image
from .geo import grid_for,Grid,read_raster
from .sources import terrain,weather,glofas,jrc_map,bracket,parse_time,river_bundle
from .fastmodel import drainage,ponding,runoff_scenarios,metrics

ROOT=Path(__file__).resolve().parents[1]; DEMO=ROOT/'demo'; NODATA=-9999.


def emit(folder,phase):
    (folder/'progress.tmp').write_text(json.dumps({'phase':phase,'updated':time.time()})); (folder/'progress.tmp').replace(folder/'progress.json')


def bbox_point(lat,lon,side):
    if not (-79.5<lat<83.5 and -179.5<lon<179.5 and 1<=side<=14): raise ValueError('Supported latitude -79.5..83.5, longitude -179.5..179.5; side 1–14 km.')
    g=Geod(ellps='WGS84'); w,_,_=g.fwd(lon,lat,270,side*500); e,_,_=g.fwd(lon,lat,90,side*500); _,s,_=g.fwd(lon,lat,180,side*500); _,n,_=g.fwd(lon,lat,0,side*500); return [w,s,e,n]


def load_demo():
    with rio.open(DEMO/'forli_dem.tif') as s:
        z=s.read(1,masked=True).astype(float).filled(np.nan); grid=Grid(z.shape,s.transform,s.crs,30,[11.98,44.20,12.10,44.32])
    # Actual previous run water volume divided by its full DEM area. No invented GloFAS/soil data.
    r=json.loads((DEMO/'baseline_v02.json').read_text()); rain=r['balance_m3']['rain']/(np.isfinite(z).sum()*900)
    with rio.open(DEMO/'forli_reference.tif') as s: ref=s.read(1,masked=True).astype(float).filled(np.nan)
    return grid,z,np.full(z.shape,rain),np.full(z.shape,np.nan),ref


def save_tif(folder,name,a,g):
    with rio.open(folder/(name+'.tif'),'w',driver='GTiff',width=g.shape[1],height=g.shape[0],count=1,dtype='float32',crs=g.crs,transform=g.transform,nodata=NODATA,compress='deflate') as f: f.write(np.where(np.isfinite(a),a,NODATA).astype('float32'),1)


def rgba(a,kind):
    out=np.zeros((*a.shape,4),np.uint8); valid=np.isfinite(a)
    if kind=='terrain':
        lo,hi=np.nanpercentile(a,[1,99]); x=np.clip((a-lo)/max(hi-lo,.01),0,1); x=np.nan_to_num(x); out[:,:,:3]=np.stack([65+x*140,85+x*135,62+x*135],-1).astype('uint8');out[:,:,3]=np.where(valid,255,0)
    elif kind=='compare':
        for k,c in {1:[37,180,128,220],2:[240,151,40,220],3:[215,70,145,220]}.items():out[a==k]=c
    elif kind=='reference':out[valid&(a==1)]=[162,94,231,205]
    elif kind=='river_network':out[valid&(a==1)]=[60,230,210,255]
    else:
        for lo,hi,c in [(0.05,.2,[107,205,251,165]),(.2,.5,[51,157,240,190]),(.5,1,[30,100,210,205]),(1,2,[23,62,160,220]),(2,np.inf,[59,32,126,235])]:out[valid&(a>=lo)&(a<hi)]=c
    return out


def save_png(folder,name,a,g,kind):
    # Leaflet overlay is Web Mercator, not raw UTM pixels on geographic rectangle.
    bounds=array_bounds(*g.shape,g.transform); tf,w,h=calculate_default_transform(g.crs,'EPSG:3857',g.shape[1],g.shape[0],*bounds)
    out=np.full((h,w),np.nan,np.float32); reproject(a.astype('float32'),out,src_transform=g.transform,src_crs=g.crs,src_nodata=np.nan,dst_transform=tf,dst_crs='EPSG:3857',dst_nodata=np.nan,resampling=Resampling.nearest)
    Image.fromarray(rgba(out,kind)).save(folder/(name+'.png'))
    b=transform_bounds('EPSG:3857','EPSG:4326',*array_bounds(h,w,tf));return [[b[1],b[0]],[b[3],b[2]]]


def run(cfg,folder):
    t=time.perf_counter(); sources={}; warnings=[]; layers={}; ref=None; mode=cfg['mode']; historic=mode=='historical'; emit(folder,'Підготовка області')
    if mode=='demo':
        g,z,rain,moist,ref=load_demo(); start=parse_time('2023-05-16T17:14:00Z');end=start+timedelta(hours=24);wc=None;river_info=None
        sources['reference']={'status':'ok','provider':'included Forlì CEMS reference raster from v0.2','observation_utc':'2023-05-17T17:14:00Z'};sources['DEM']={'status':'ok','provider':'Uploaded Forlì DEM from v0.2'};sources['weather']={'status':'ok','provider':'previous ERA5 run; uniform cumulative rain','rain_mm':float(rain.mean()*1000)};sources['GloFAS']={'status':'unavailable','reason':'offline replay: no event discharge history supplied'};sources['WorldCover']={'status':'unavailable','reason':'offline replay: land cover raster not supplied'}
    else:
        bbox=cfg.get('bbox') or bbox_point(cfg['lat'],cfg['lon'],cfg['side_km']);g=grid_for(bbox);start=parse_time(cfg['start_utc']) if historic else datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0);end=start+timedelta(hours=cfg['hours'])
        if historic and end>datetime.now(timezone.utc):raise ValueError('Historical end must be in the past.')
        emit(folder,'Паралельне отримання DEM, WorldCover, опадів і GloFAS')
        results={}
        with ThreadPoolExecutor(max_workers=5) as pool:
            tasks={'DEM':pool.submit(terrain,g,'dem'),'WorldCover':pool.submit(terrain,g,'worldcover'),'weather':pool.submit(weather,g,start,end,historic),'GloFAS':pool.submit(river_bundle,g,start,end),'JRC_RP10':pool.submit(jrc_map,g,10)}
            for name,f in tasks.items():
                try: results[name]=f.result(); sources[name]={'status':'ok'}
                except Exception as e: sources[name]={'status':'unavailable','reason':str(e)[:250]}
        if 'DEM' not in results: raise ValueError('DEM unavailable: '+sources['DEM']['reason'])
        z,meta=results['DEM'];sources['DEM'].update(meta)
        if not np.isfinite(z).any():raise ValueError('No valid Copernicus DEM for area: '+str(meta.get('errors',[]))[:250])
        if meta.get('errors') or np.isfinite(z).mean()<.999: warnings.append('DEM частково відсутній; nodata не вважається сухою землею.')
        wc=None
        if 'WorldCover' in results:
            wc,meta=results['WorldCover'];sources['WorldCover'].update(meta)
            if not np.isfinite(wc).any():wc=None;sources['WorldCover']['status']='unavailable'
            elif not np.isfinite(wc).all():sources['WorldCover']['status']='partial'
        rain=moist=None
        if 'weather' in results:
            rain,moist,meta=results['weather'];sources['weather'].update(meta)
            if meta['partial']:warnings.append('Частина погодних точок недоступна; використано наявні комірки.')
        river_info=None
        if 'GloFAS' in results:
            river_info,osm,osm_meta,error=results['GloFAS'];sources['OSM_rivers']=osm_meta
            if osm is not None: layers['river_network']=osm
            if river_info:sources['GloFAS'].update(river_info)
            else:sources['GloFAS']={'status':'unavailable','reason':error}
            if osm_meta['status']!='ok':warnings.append('Геометрія OSM недоступна; GloFAS запитано за точками сітки.')
        if 'JRC_RP10' in results:
            a,meta=results['JRC_RP10'];sources['JRC_RP10'].update(meta);layers['jrc_rp10_context']=a
            if not np.isfinite(a).any():sources['JRC_RP10']['status']='unavailable'
        if (folder/'reference_upload.tif').exists():
            with rio.open(folder/'reference_upload.tif') as rr:
                raw=rr.read(1,masked=True).astype(float).filled(np.nan)
                if not np.isin(raw[np.isfinite(raw)],[0,1]).all():raise ValueError('Reference must contain 0/1 and NoData; depth raster is not a reference mask.')
                if rr.nodata is None and not cfg.get('full_reference'):raise ValueError('Reference has no NoData; confirm full coverage or supply masked GeoTIFF.')
            ref=read_raster((folder/'reference_upload.tif').read_bytes(),g)
        elif historic and g.bbox==[11.98,44.2,12.1,44.32] and start==parse_time('2023-05-16T17:14:00Z') and end==parse_time('2023-05-17T17:14:00Z'):
            ref=read_raster((DEMO/'forli_reference.tif').read_bytes(),g); sources['reference']={'status':'ok','provider':'included Forlì CEMS reference raster from v0.2','observation_utc':'2023-05-17T17:14:00Z'}
    valid=np.isfinite(z); permanent=(wc==80) if wc is not None else np.zeros(g.shape,bool); area_mask=valid&~permanent
    if wc is None or not np.isfinite(wc[valid]).all():warnings.append('Постійні водойми не повністю визначено; площа може включати воду, яка вже існувала.')
    if rain is not None:
        emit(folder,'Швидке розтікання стоку в пониженнях DEM'); tt=time.perf_counter(); prepared=drainage(z,valid); balances={}
        for name,f in zip(('rain_low','rain_mid','rain_high'),runoff_scenarios(wc,moist)):
            a,b=ponding(z,valid,rain,f,prepared=prepared);layers[name]=a;balances[name]=b
        sources['rain_model']={'status':'ok','method':'D8 priority-flood depression storage; no time stepping; runoff fractions illustrative','seconds':time.perf_counter()-tt,'balances':balances,'soil_moisture_coverage':float(np.isfinite(moist[valid]).mean())}
        if not np.isfinite(moist[valid]).all():warnings.append('Вологість ґрунту частково/повністю відсутня: використано явно умовний середній коефіцієнт стоку та широкий сценарний діапазон.')
    else:warnings.append('Опади недоступні: дощовий компонент не розраховано.')
    river_ready=False; river_partial=False
    if river_info and bracket(river_info['regional_rp']):
        lo,hi=bracket(river_info['regional_rp']);emit(folder,'Вибір готових річкових карт JRC')
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                f={rp:pool.submit(jrc_map,g,rp) for rp in {lo,hi} if not (rp==10 and 'jrc_rp10_context' in layers)}
                qc=pool.submit(jrc_map,g,'spurious-depths')
                maps={rp:(layers['jrc_rp10_context'] if rp==10 and 'jrc_rp10_context' in layers else f[rp].result()[0]) for rp in {lo,hi}}
                try:sp,sp_meta=qc.result();bad=np.isfinite(sp)&(sp>0);sources['JRC_QC']={'status':'ok',**sp_meta}
                except Exception as ex:bad=np.zeros(g.shape,bool);sources['JRC_QC']={'status':'unavailable','reason':str(ex)[:200]};warnings.append('Маска сумнівних глибин JRC недоступна.')
            a,b=maps[lo],maps[hi];ok=valid&np.isfinite(a)&np.isfinite(b)&~bad
            if not ok.any():raise ValueError('No overlapping valid JRC pixels')
            # Regional scenario, not independently matched reach-specific hydraulic boundary.
            a=np.where(ok,np.maximum(a,0),np.nan);b=np.where(ok,np.maximum(b,a),np.nan);w=0 if lo==hi else np.clip(math.log(river_info['regional_rp']/lo)/math.log(hi/lo),0,1)
            layers.update(river_low=a,river_mid=a+(b-a)*w,river_high=b);river_ready=True;river_partial=not ok[area_mask].all();sources['JRC_event']={'status':'partial' if river_partial else 'ok','rp_bracket':[lo,hi],'regional_rp':river_info['regional_rp'],'native_resolution_m':90,'coverage':float(ok[valid].mean()),'method':'regional maximum empirical RP applied to static hazard maps; not reach-specific forecast; log-RP interpolation heuristic'}
            warnings.append('Річкова карта — регіональний сценарій, не прогноз конкретного русла; історичні максимуми добові, піки всередині доби невідомі.')
        except Exception as ex:sources['JRC_event']={'status':'unavailable','reason':str(ex)[:250]}
    elif river_info:sources['JRC_event']={'status':'unavailable','reason':'empirical RP <10: published JRC catalogue begins at RP10; smaller floods unknown'}
    else:sources['JRC_event']={'status':'unavailable','reason':'No verified usable discharge series'}
    if not river_ready:warnings.append('Річковий компонент невідомий. Не трактувати відсутність синього як відсутність повені. RP10, якщо є, лише статична карта небезпеки.')
    rain_ready='rain_mid' in layers
    if not rain_ready and not river_ready: raise ValueError('No event component available; '+str({k:v.get('reason') for k,v in sources.items() if v.get('status')=='unavailable'})[:750])
    emit(folder,'Побудова карт та метрик');
    for tag in ('low','mid','high'):
        a=layers.get('rain_'+tag);b=layers.get('river_'+tag)
        if a is None: h=b.copy()
        elif b is None:h=a.copy()
        else:h=np.fmax(a,b) # screening union; missing component explicitly marked
        h[~area_mask]=np.nan;layers[tag]=h
    # Coverage completeness is a separate layer; missing river is not a dry cell.
    complete=area_mask&rain_ready&river_ready
    if river_ready:complete=complete&np.isfinite(layers['river_mid'])
    if wc is None:complete=np.zeros(g.shape,bool)
    elif rain_ready:complete=complete&np.isfinite(wc)&np.isfinite(moist)
    layers['coverage_complete']=np.where(valid,complete.astype(float),np.nan)
    if ref is not None:layers['reference']=ref
    stats={}; threshold=.05
    for n in ('low','mid','high','rain_mid','river_mid','jrc_rp10_context'):
        if n in layers:
            a=layers[n]; ok=area_mask&np.isfinite(a);stats[n]={'area_km2':float((ok&(a>=threshold)).sum()*.0009),'evaluated_km2':float(ok.sum()*.0009),'max_depth_m':float(np.max(a[ok])) if ok.any() else None}
    m=None
    if ref is not None:
        m=metrics(layers['mid'],ref,area_mask,threshold); ok=area_mask&np.isfinite(ref)&np.isfinite(layers['mid']);p=layers['mid']>=threshold;tref=ref==1
        layers['compare']=np.where(ok,np.where(p&tref,1,np.where(p&~tref,2,np.where(~p&tref,3,0))),np.nan)
        warnings.append('Метрики лише діагностичні: швидка модель оцінює накопичення/сценарний максимум, еталон може бути миттєвим знімком.')
    overlays={}; bounds=None
    layers['terrain']=z
    for n,a in layers.items():
        save_tif(folder,n,a,g)
        if n=='coverage_complete':continue
        bounds=save_png(folder,n,a,g,'terrain' if n=='terrain' else n if n in ('compare','reference','river_network') else 'depth');overlays[n]=n+'.png'
    report=dict(version='Rapid 0.1',mode=mode,start_utc=start.isoformat(),end_utc=end.isoformat(),cell_m=30,shape=list(g.shape),crs=str(g.crs),bbox=g.bbox,overlay_bounds=bounds,threshold_m=.05,components={'rain':rain_ready,'river':river_ready,'river_partial':river_partial},status='scenario_estimate' if rain_ready and river_ready and not river_partial and complete[area_mask].all() else 'partial_scenario',areas=stats,domain_km2=float(valid.sum()*.0009),full_input_coverage_km2=float(complete.sum()*.0009),metrics=m,sources=sources,warnings=warnings,overlays=overlays,runtime_s=time.perf_counter()-t,baseline_v02={'iou':.16325646579182063,'f1':.2806886883378603,'runtime_s':440.68400717500026} if mode=='demo' else None,disclaimer='Research screening only, not operational warning. No calibrated flood probability; scenario interval is not statistical confidence. No dynamic flow, drainage, river bathymetry, defenses, tidal flooding, snowmelt or validated generalization. Total precipitation treated as liquid water.')
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False));emit(folder,'Готово');return report

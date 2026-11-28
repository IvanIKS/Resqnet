import json,time,calendar
from datetime import datetime,timedelta,timezone
import numpy as np
import pytest
import rasterio as rio
from fastapi.testclient import TestClient
from rapid.fastmodel import drainage,ponding,metrics,runoff_scenarios
from rapid.sources import weather_totals,annual_maxima,empirical_rp,bracket,parse_time
from rapid.engine import run,bbox_point,load_demo
from rapid.geo import grid_for
from rapid.app import app


def test_basin_mass_and_spill():
    z=np.full((9,9),4.);z[2:7,2:7]=0.;z[0,:]=0.;z[1,4]=2.;z[2,4]=1.;ok=np.ones(z.shape,bool)
    a,b=ponding(z,ok,.5,1.)
    assert abs(b['residual_m3'])<.01 and (a>=0).all() and a[4,4]>0
    assert (a<=4).all()
    big,c=ponding(z,ok,10,1.);assert c['exported_m3']>0 and abs(c['residual_m3'])<.01
    assert (big>=a-1e-6).all()


def test_open_slope_no_ponds():
    z=np.tile(np.arange(15,dtype=float),(12,1));a,b=ponding(z,np.ones(z.shape,bool),.1,1.)
    assert np.max(a)==0 and b['ponded_m3']==0 and abs(b['runoff_m3']-b['exported_m3'])<1e-6


def test_nodata():
    z=np.zeros((5,5));z[2,2]=np.nan;v=np.isfinite(z);a,b=ponding(z,v,.1,1.)
    assert np.isnan(a[2,2]) and abs(b['residual_m3'])<.01


def test_weather_precise_interval():
    j={'latitude':44.,'longitude':12.,'hourly_units':{'precipitation':'mm'},'hourly':{'time':['2023-05-16T18:00','2023-05-16T19:00','2023-05-16T20:00'],'precipitation':[6.,12.,0.],'soil_moisture_0_to_7cm':[.3,.35,.4]}}
    r=weather_totals(j,parse_time('2023-05-16T17:30:00Z'),parse_time('2023-05-16T18:30:00Z'),'soil_moisture_0_to_7cm');assert r['rain_mm']==9 and r['moisture'] is None
    with pytest.raises(ValueError):weather_totals(j,parse_time('2023-05-16T16:00:00Z'),parse_time('2023-05-16T18:00:00Z'),'soil_moisture_0_to_7cm')
    assert parse_time('2023-05-16T17:00:00-02:00').hour==19


def test_annual_rank():
    t=[];q=[]
    for y in range(2000,2016):
        d=datetime(y,1,1)
        for i in range(365+calendar.isleap(y)):
            t.append((d+timedelta(days=i)).date().isoformat());q.append(y-1999)
    j={'daily_units':{'river_discharge':'m³/s'},'daily':{'time':t,'river_discharge':q}}
    a=annual_maxima(j,2000,2015);assert len(a)==16 and a.max()==16
    assert empirical_rp(a,0)==1 and empirical_rp(a,99)==17 and empirical_rp(a[:3],3) is None
    assert bracket(9) is None and bracket(17)==(10,20) and bracket(20)==(20,20)


def test_fixed_grid():
    g=grid_for(bbox_point(44.26,12.04,10));assert g.dx==30
    with pytest.raises(ValueError):grid_for([11.98,44.2,12.1,44.32],10)


def test_actual_forli(tmp_path):
    r=run({'mode':'demo'},tmp_path);assert r['shape']==[456,336] and r['metrics']['observed_km2']==pytest.approx(12.0717)
    assert r['status']=='partial_scenario' and not r['components']['river'];assert r['full_input_coverage_km2']==0
    assert r['areas']['low']['area_km2']<=r['areas']['mid']['area_km2']<=r['areas']['high']['area_km2']
    assert 0<r['metrics']['iou']<1
    with rio.open(tmp_path/'mid.tif') as s:assert s.res==(30.,30.) and s.crs.to_epsg()==32633


def test_online_mock_combines_sources(tmp_path,monkeypatch):
    import rapid.engine as e
    def tr(g,kind):
        a=np.full(g.shape,40. if kind=='worldcover' else 10.);return a,{'coverage':1.,'errors':[]}
    def weather(g,s,t,h):return np.full(g.shape,.03),np.full(g.shape,.3),{'partial':False,'points':[]}
    def rivers(g,s,t):return {'regional_rp':17,'points':[{'peak_daily_m3s':30}]},np.zeros(g.shape),{'status':'ok'},None
    def hazard(g,rp):
        a=np.full(g.shape,0. if rp=='spurious-depths' else rp/100.);return a,{'coverage':1.}
    monkeypatch.setattr(e,'terrain',tr);monkeypatch.setattr(e,'weather',weather);monkeypatch.setattr(e,'river_bundle',rivers);monkeypatch.setattr(e,'jrc_map',hazard)
    cfg={'mode':'historical','lat':44.26,'lon':12.04,'side_km':1,'hours':24,'start_utc':'2023-05-16T17:14:00Z'}
    r=run(cfg,tmp_path);assert r['components']['river'] and r['components']['rain'] and r['status']=='scenario_estimate'
    assert r['sources']['JRC_event']['rp_bracket']==[10,20] and r['full_input_coverage_km2']==r['domain_km2']
    with rio.open(tmp_path/'mid.tif') as s:a=s.read(1);assert np.all((a>=.1)&(a<=.2))


def test_missing_source_not_dry(tmp_path,monkeypatch):
    import rapid.engine as e
    def tr(g,kind):return np.ones(g.shape),{'coverage':1.,'errors':[]}
    def weather(g,s,t,h):return np.zeros(g.shape),np.full(g.shape,.3),{'partial':False,'points':[]}
    def fail(*a):raise RuntimeError('mock unavailable')
    monkeypatch.setattr(e,'terrain',tr);monkeypatch.setattr(e,'weather',weather);monkeypatch.setattr(e,'river_bundle',fail);monkeypatch.setattr(e,'jrc_map',fail)
    r=run({'mode':'forecast','lat':44.26,'lon':12.04,'side_km':1,'hours':24},tmp_path)
    assert r['status']=='partial_scenario' and r['areas']['mid']['area_km2']==0 and not r['components']['river'] and r['full_input_coverage_km2']==0


def test_http_job():
    c=TestClient(app);assert c.get('/').status_code==200 and c.get('/api/health').json()['resolution_m']==30
    assert c.post('/api/jobs',data={'config':'{"mode":"forecast","side_km":50}'}).status_code==400
    r=c.post('/api/jobs',data={'config':'{"mode":"demo"}'});assert r.status_code==200;id=r.json()['id']
    for _ in range(120):
        s=c.get('/api/jobs/'+id).json()
        if s['state']!='running':break
        time.sleep(.2)
    assert s['state']=='done',s
    assert c.get(f'/api/jobs/{id}/files/mid.tif').status_code==200
    assert c.get(f'/api/jobs/{id}/download').status_code==200

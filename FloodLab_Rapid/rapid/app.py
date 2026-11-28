import os,sys,json,uuid,subprocess,threading,time,zipfile
from pathlib import Path
from typing import Literal
from fastapi import FastAPI,HTTPException,Form,UploadFile,File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from .engine import ROOT, bbox_point
from .geo import grid_for
from .sources import parse_time

app=FastAPI(title='FloodLab Rapid 0.1'); app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static'); RESULTS=ROOT/'results';RESULTS.mkdir(exist_ok=True)
lock=threading.Lock(); active=set(); max_jobs=2; LIMIT=120
class Config(BaseModel):
    mode: Literal['forecast','historical','demo']='forecast'
    lat:float=44.26;lon:float=12.04;side_km:float=Field(10,ge=1,le=14)
    bbox:list[float]|None=None; hours:Literal[24,48,72]=24;start_utc:str='2023-05-16T17:14:00Z';full_reference:bool=False

@app.get('/')
def index():return FileResponse(ROOT/'static/index.html')
@app.get('/api/health')
def health():return {'version':'Rapid 0.1','resolution_m':30,'deadline_seconds':LIMIT,'cpu_available':len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else os.cpu_count(),'jobs':len(active),'paid_key_configured':bool(os.getenv('OPEN_METEO_API_KEY'))}

def get_folder(id):
    try:
        if uuid.UUID(id).hex!=id:raise ValueError()
    except ValueError:raise HTTPException(404,'Unknown job')
    p=RESULTS/id
    if not p.is_dir():raise HTTPException(404,'Unknown job')
    return p

def supervise(p,id):
    try:
        env=dict(os.environ);env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
        # Worker is a subprocess: hard wall-clock budget, no runaway simulation.
        proc=subprocess.Popen([sys.executable,'-m','rapid.worker',str(p)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:proc.wait(timeout=LIMIT)
        except subprocess.TimeoutExpired:proc.kill();proc.wait();(p/'error.json').write_text(json.dumps({'error':f'Перевищено ліміт {LIMIT} с: мережеві дані/обчислення не завершилися. Спробуй меншу ділянку або повтори пізніше. Частковий результат не видано.'},ensure_ascii=False))
        if not (p/'report.json').exists() and not (p/'error.json').exists():(p/'error.json').write_text(json.dumps({'error':f'Worker exit {proc.returncode}'}))
    except Exception as ex:(p/'error.json').write_text(json.dumps({'error':str(ex)[:300]}))
    finally:
        with lock:active.discard(id)

@app.post('/api/jobs')
async def submit(config:str=Form(...),reference:UploadFile|None=File(None)):
    try:
        cfg=Config.model_validate_json(config)
        if cfg.mode!='demo':
            g=grid_for(cfg.bbox or bbox_point(cfg.lat,cfg.lon,cfg.side_km))
            if cfg.mode=='historical':parse_time(cfg.start_utc)
        data=await reference.read(8_000_001) if reference else None
        if data and len(data)>8_000_000:raise ValueError('Reference exceeds 8 MB.')
    except Exception as ex:raise HTTPException(400,str(ex)[:400])
    with lock:
        if len(active)>=max_jobs:raise HTTPException(429,'Two jobs already running.')
        id=uuid.uuid4().hex;active.add(id)
    p=RESULTS/id;p.mkdir();(p/'config.json').write_text(cfg.model_dump_json());(p/'progress.json').write_text(json.dumps({'phase':'Запуск'}))
    if data:(p/'reference_upload.tif').write_bytes(data)
    threading.Thread(target=supervise,args=(p,id),daemon=True).start();return {'id':id}
@app.get('/api/jobs/{id}')
def status(id:str):
    p=get_folder(id)
    if (p/'error.json').exists():return {'state':'failed',**json.loads((p/'error.json').read_text())}
    if (p/'report.json').exists():return {'state':'done','report':json.loads((p/'report.json').read_text())}
    return {'state':'running',**json.loads((p/'progress.json').read_text())}
@app.get('/api/jobs/{id}/files/{name}')
def file(id:str,name:str):
    p=get_folder(id)
    if name not in ('report.json',) and not (name.endswith(('.tif','.png')) and name!='reference_upload.tif' and name==Path(name).name):raise HTTPException(404)
    if not (p/name).is_file() or not (p/'report.json').exists():raise HTTPException(404)
    return FileResponse(p/name,filename=name if name.endswith(('.tif','.json')) else None)
@app.get('/api/jobs/{id}/download')
def download(id:str):
    p=get_folder(id)
    if not (p/'report.json').exists():raise HTTPException(404)
    z=p/'results.zip'
    if not z.exists():
        with zipfile.ZipFile(z,'w',zipfile.ZIP_DEFLATED) as f:
            for a in p.iterdir():
                if a.name=='report.json' or (a.suffix in ('.tif','.png') and a.name!='reference_upload.tif'):f.write(a,a.name)
    return FileResponse(z,filename='FloodLab_Rapid_results.zip')

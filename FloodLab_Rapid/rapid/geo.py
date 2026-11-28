import math, json, os, io, zipfile, tempfile
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import rasterio as rio
from rasterio.warp import reproject, Resampling, transform_bounds
from rasterio.transform import from_origin
from rasterio.features import rasterize
from rasterio.io import MemoryFile
from pyproj import CRS
import requests

@dataclass
class Grid:
    shape: tuple
    transform: object
    crs: object
    dx: float
    bbox: list


def grid_for(bbox, dx=30):
    if dx!=30: raise ValueError("FloodLab grid is fixed at 30 m.")
    w,s,e,n=map(float,bbox)
    if not (-180<=w<e<=180 and -80<=s<n<=84): raise ValueError('Use west,south,east,north, within UTM latitudes; no antimeridian crossing.')
    if e-w>2 or n-s>2: raise ValueError('MVP domain limited to 2° in either direction.')
    lon=(w+e)/2; lat=(s+n)/2; zone=min(60,max(1,int((lon+180)//6)+1)); crs=CRS.from_epsg((32600 if lat>=0 else 32700)+zone)
    left,bottom,right,top=transform_bounds('EPSG:4326',crs,w,s,e,n,densify_pts=21)
    nx=math.ceil((right-left)/dx); ny=math.ceil((top-bottom)/dx)
    if nx<3 or ny<3 or nx*ny>250000: raise ValueError(f'Grid {nx} × {ny}: need 3 cells each direction, max 250,000 cells. Reduce area (resolution fixed at 30 m).')
    return Grid((ny,nx),from_origin(left,top,dx,dx),crs,dx,[w,s,e,n])


def tile_urls(bbox, source):
    w,s,e,n=bbox; step=3 if source=='worldcover' else 1
    for lat in range(math.floor(s/step)*step,math.ceil(n/step)*step,step):
        for lon in range(math.floor(w/step)*step,math.ceil(e/step)*step,step):
            ns=f'{"N" if lat>=0 else "S"}{abs(lat):02}'; ew=f'{"E" if lon>=0 else "W"}{abs(lon):03}'
            if source=='worldcover': yield f'https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{ns}{ew}_Map.tif'
            else:
                name=f'Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM'; yield f'https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif'


def project(src, grid, method):
    if not src.crs: raise ValueError('Raster must contain a valid CRS.')
    out=np.full(grid.shape,np.nan,dtype=np.float32)
    reproject(rio.band(src,1),out,src_transform=src.transform,src_crs=src.crs,src_nodata=src.nodata,dst_transform=grid.transform,dst_crs=grid.crs,dst_nodata=np.nan,resampling=method)
    return out


def read_raster(data,grid,method=Resampling.nearest):
    with MemoryFile(data) as mem, mem.open() as src: return project(src,grid,method)


def remote(grid,source):
    result=np.full(grid.shape,np.nan,np.float32); errors=[]; urls=[]
    # Cover the full projected rectangular grid, not only the requested geographic bbox.
    from rasterio.transform import array_bounds
    b=transform_bounds(grid.crs,'EPSG:4326',*array_bounds(*grid.shape,grid.transform),densify_pts=21)
    for url in tile_urls(b,source):
        try:
            with rio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR',CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.tif',GDAL_HTTP_TIMEOUT='15',GDAL_HTTP_CONNECTTIMEOUT='8',GDAL_HTTP_MAX_RETRY='0'):
                with rio.open(url) as src: a=project(src,grid,Resampling.nearest if source=='worldcover' else Resampling.bilinear)
            mask=np.isfinite(a); result[mask]=a[mask]; urls.append(url)
        except Exception as ex: errors.append(f'{url.rsplit("/",1)[-1]}: {str(ex)[:180]}')
    return result,urls,errors


def roughness(classes,default):
    out=np.full(classes.shape,default,dtype=np.float64)
    # Indicative engineering assumptions, not calibrated local Manning coefficients.
    for k,v in {10:.10,20:.07,30:.035,40:.045,50:.08,60:.025,70:.025,80:.03,90:.075,95:.10,100:.035}.items(): out[classes==k]=v
    return out


def demo(dx=30):
    if dx!=30: raise ValueError("FloodLab grid is fixed at 30 m.")
    ny,nx=64,80; y,x=np.mgrid[:ny,:nx]; r=((x-40)/21)**2+((y-32)/17)**2
    z=100+0.9*r+0.07*np.sin(x/5)*np.sin(y/7)
    # Predefined synthetic reference: independent hydrostatic water stage with known wet mask.
    stage=100.55; initial=np.maximum(stage-z,0.)
    grid=Grid(z.shape,from_origin(0,ny*dx,dx,dx),None,dx,[])
    return grid,z,initial


def vector_mask(data, filename, grid):
    if not grid.crs: raise ValueError('Geographic reference cannot be used with synthetic coordinates.')
    from rasterio.warp import transform_geom
    import fiona
    geoms=[]
    if filename.lower().endswith(('.json','.geojson')):
        obj=json.loads(data)
        if obj.get('crs') is not None: raise ValueError('GeoJSON must use standard WGS84 longitude/latitude; convert other CRS first.')
        items=obj.get('features',[]) if obj.get('type')=='FeatureCollection' else [obj]
        for item in items:
            g=item.get('geometry') if item.get('type')=='Feature' else item
            if g and g.get('type') in ('Polygon','MultiPolygon'): geoms.append(transform_geom('EPSG:4326',grid.crs,g))
    elif filename.lower().endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist())>300_000_000: raise ValueError('ZIP uncompressed size exceeds 300 MB.')
            candidates=[i for i in z.namelist() if i.lower().endswith('.shp') and 'observedeventa' in i.lower()]
            if len(candidates)!=1: raise ValueError('ZIP must have exactly one observedEventA shapefile; export the chosen flood polygons as GeoJSON if multiple.')
            with tempfile.TemporaryDirectory() as d:
                base=Path(candidates[0]).stem.lower()
                for i in z.infolist():
                    p=Path(i.filename)
                    if p.stem.lower()==base and p.suffix.lower() in ('.shp','.shx','.dbf','.prj','.cpg'):
                        (Path(d)/(base+p.suffix.lower())).write_bytes(z.read(i))
                with fiona.open(Path(d)/(base+'.shp')) as src:
                    if not src.crs: raise ValueError('Shapefile missing CRS.')
                    for f in src:
                        g=f['geometry']; props=dict(f['properties']);
                        if g and g['type'] in ('Polygon','MultiPolygon'):
                            geoms.append(transform_geom(src.crs,grid.crs,dict(g)))
    else: raise ValueError('Reference vector: use GeoJSON or CEMS ZIP with one observedEventA shapefile.')
    if not geoms: raise ValueError('No polygon geometries found.')
    return rasterize(geoms,out_shape=grid.shape,transform=grid.transform,fill=0,default_value=1,dtype='uint8').astype(float)


def glofas(lat,lon,start,end):
    from datetime import date
    a=date.fromisoformat(start); b=date.fromisoformat(end)
    if not 0<=(b-a).days<=31: raise ValueError('Date window must be 1–32 days.')
    key=os.getenv('OPEN_METEO_API_KEY'); url='https://customer-flood-api.open-meteo.com/v1/flood' if key else 'https://flood-api.open-meteo.com/v1/flood'
    p=dict(latitude=lat,longitude=lon,start_date=start,end_date=end,daily='river_discharge',timezone='GMT')
    if key: p['apikey']=key
    r=requests.get(url,params=p,timeout=25); r.raise_for_status(); j=r.json()
    return {k:j.get(k) for k in ['latitude','longitude','daily','daily_units']}


def cems(code):
    import re
    if not re.fullmatch(r'EMSR\d{3,5}',code.upper()): raise ValueError('Expected EMSR activation code, e.g. EMSR847.')
    url='https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations/'
    r=requests.get(url,params={'code':code.upper()},timeout=25); r.raise_for_status(); j=r.json(); out=[]
    for act in j.get('results',[]):
        for a in act.get('aois',[]):
            for p in a.get('products',[]):
                out.append(dict(aoi=a.get('name'),type=p.get('type'),id=p.get('id'),monitoring=p.get('monitoringNumber'),download=p.get('downloadPath'),images=p.get('images'),extent=p.get('extent') or a.get('extent'),version=p.get('version'),feasible=p.get('feasible')))
    return out

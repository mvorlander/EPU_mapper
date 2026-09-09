"""Lazy acquisition/FoilHole-only dashboard; no Data MRC discovery or loading."""
from contextlib import asynccontextmanager
import io
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, Response
from acquisition_store import AcquisitionStore
from acquisition_ui import PAGE


def create_acquisition_app(source, atlas=None, mode='acquisition', transform='identity', label=None, cache_root=None):
    if mode not in ('acquisition','foilhole'):
        raise ValueError('Unknown acquisition mode')
    store = AcquisitionStore(source,atlas,mode,cache_root=cache_root)

    @asynccontextmanager
    async def lifespan(app):
        store.set_meta('atlas',None)
        if not store.meta('indexed:'+mode):
            store.start_scan()
        elif store.atlas:
            store.scan_state.update(status='indexing',message='Loading selected Atlas…')
            def load_atlas():
                try:
                    store._index_atlas(refresh=True)
                    store.scan_state.update(status='ready',message='Selected Atlas ready · cached acquisition index')
                except Exception as exc:
                    store.scan_state.update(status='error',message='Could not load selected Atlas: '+str(exc))
                    raise
            store.submit('Loading selected Atlas',load_atlas)
        yield
        store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.acquisition_store = store
    latest_images = {}

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail':str(exc)},status_code=404)

    @app.get('/',response_class=HTMLResponse)
    def home():
        config = json.dumps(dict(mode=mode,label=label or Path(source).name,transform=transform or 'identity')).replace('<','\\u003c')
        return PAGE.replace('__CONFIG__',config)

    @app.get('/api/status')
    def status():
        return dict(mode=mode,index=dict(store.scan_state),grids=store.grids(),atlas=store.meta('atlas'),cache=store.cache_status())

    @app.post('/api/refresh')
    def refresh():
        return dict(started=store.start_scan(refresh=True))

    @app.get('/api/holes/{gid}')
    def holes(gid: str, offset: int=Query(0,ge=0), limit: int=Query(100,ge=1,le=200), query: str=''):
        store.grid(gid)
        return store.holes(gid,offset,limit,query)

    @app.get('/api/exposures/{gid}/{hole}')
    def exposures(gid: str,hole: str):
        store.grid(gid)
        return store.exposures(gid,hole)

    @app.post('/api/geometry/{gid}')
    def geometry(gid: str):
        store.grid(gid)
        return dict(job=store.submit('Loading hole positions',lambda:store.geometry(gid,transform)))

    @app.post('/api/areas/{gid}')
    def acquisition_areas(gid: str):
        store.grid(gid)
        return dict(job=store.submit('Loading planned Data areas',lambda:store.acquisition_areas(gid,transform)))

    @app.post('/api/prepare/{key}')
    def prepare(key: str,value: dict):
        store.media(key)
        lane = value.get('viewer','')
        if lane not in ('','atlas','grid','foil','data'):
            raise HTTPException(400,'Invalid viewer')
        if lane:
            latest_images[lane] = key
        if store.local_file(key):
            return dict(ready=True)
        def load():
            if lane and latest_images.get(lane)!=key:
                return None
            return str(store.cache_file(key))
        return dict(job=store.submit('Loading selected image',load))

    @app.get('/api/jobs/{key}')
    def job(key: str):
        if key not in store.jobs:
            raise HTTPException(404,'Unknown or expired job')
        return dict(store.jobs[key])

    @app.get('/api/image/{key}')
    def image(key: str, adjust: bool=False, low: float=1, high: float=99, gamma: float=1, sigma: float=0, routine: str='percentile'):
        media = store.media(key)
        local = store.local_file(key)
        if not local:
            raise HTTPException(409,'Preview not cached yet; load the image first')
        if media['kind'] not in ('atlas','grid','foil','data','atlas_mrc','grid_mrc'):
            raise HTTPException(403,'Not a display image')
        if adjust or media['kind'].endswith('_mrc'):
            from image_adjustments import adjusted_preview
            try:
                rendered = adjusted_preview(local,low=low,high=high,gamma=gamma,sigma=sigma,mode=routine)
            except ValueError as exc:
                raise HTTPException(400,str(exc)) from exc
            output=io.BytesIO();rendered.save(output,format='JPEG',quality=92)
            return Response(output.getvalue(),media_type='image/jpeg')
        return FileResponse(local)

    def validate_annotation(key):
        parts=key.split(':')
        if len(parts)==2 and parts[0]=='grid':
            store.grid(parts[1])
        elif len(parts)==2 and parts[0]=='exposure':
            if store.media(parts[1])['kind']!='data':
                raise HTTPException(400,'Not a Data exposure')
        elif len(parts)==3 and parts[0]=='hole':
            if not store.execute("SELECT id FROM media WHERE grid_id=? AND hole=? AND kind='foil' LIMIT 1",parts[1:]) and not (not store.ignore_data and store.execute("SELECT id FROM media WHERE grid_id=? AND hole=? AND kind='data' LIMIT 1",parts[1:])):
                raise HTTPException(404,'Unknown FoilHole')
        else:
            raise HTTPException(400,'Invalid annotation target')

    @app.get('/api/annotation/{key}')
    def annotation(key: str):
        validate_annotation(key)
        return store.annotation(key)

    @app.post('/api/annotation/{key}')
    def annotate(key: str,value: dict):
        validate_annotation(key)
        try:
            return store.annotate(key,value)
        except (TypeError,ValueError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get('/api/annotations.json')
    def download_annotations():
        return Response(json.dumps(dict(source=str(store.source),mode=mode,annotations=store.annotation_snapshot()),indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="acquisition-annotations.json"'})

    @app.post('/api/cache-previews')
    def cache_previews():
        # A single background worker performs sequential reads, leaving the
        # other worker available for foreground image requests. Restart resumes.
        def run():
            allowed=('atlas','grid','foil') if store.ignore_data else ('atlas','grid','foil','data')
            rows=store.execute('SELECT id FROM media WHERE kind IN ('+','.join('?' for _ in allowed)+')',allowed)
            failures=[]
            for row in rows:
                if store.closed:
                    break
                try:
                    store.cache_file(row['id'])
                except OSError as exc:
                    failures.append(dict(id=row['id'],error=str(exc)))
            return dict(total=len(rows),missing=failures)
        if store.scan_state['status'] in ('new','indexing'):
            raise HTTPException(409,'Wait for indexing to finish before preparing previews')
        return dict(job=store.submit('Preparing local previews (no MRCs)',run,priority=10))

    from position_corrections import install_position_corrections
    install_position_corrections(app,store,transform)
    if mode == 'acquisition':
        from cryosparc_density_app import install_density
        install_density(app,source,label,transform)
    return app

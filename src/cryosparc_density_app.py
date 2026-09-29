"""Optional particle-density routes for the acquisition dashboard."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
from fastapi import File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse
from acquisition_ui import PAGE
from cryosparc_density import DensityIndex
from native_dialog import choose_path


def inspect_cs(path, suggest_passthrough=False, required=None):
    import numpy as np
    path=Path(path).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower()!='.cs':raise ValueError('Choose a CryoSPARC .cs file.')
    table=np.load(path,allow_pickle=False,mmap_mode='r')
    names=set(table.dtype.names or ())
    required=set(required or {'location/micrograph_path','location/center_x_frac','location/center_y_frac'})
    missing=sorted(required-names);suggestion=None
    if suggest_passthrough and missing:
        candidates=[]
        for candidate in path.parent.glob('*passthrough*.cs'):
            try:
                other=np.load(candidate,allow_pickle=False,mmap_mode='r')
                other_names=set(other.dtype.names or ())
                if 'uid' in other_names and set(missing).issubset(other_names):candidates.append(candidate.resolve())
            except (OSError,ValueError):
                continue
        if len(candidates)==1:suggestion=str(candidates[0])
    return dict(path=str(path),name=path.name,particles=len(table),size=path.stat().st_size,
                has_uid='uid' in names,fields=sorted(required&names),missing=missing,
                needs_passthrough=bool(missing),suggestion=suggestion)


def install_density(app, source, label=None, transform='identity'):
    store=app.state.acquisition_store
    density=DensityIndex(store);app.state.density=density
    store.density=density
    app.router.routes[:]=[r for r in app.router.routes if getattr(r,'path',None)!='/']

    @app.get('/',response_class=HTMLResponse)
    def home():
        config=json.dumps(dict(mode=store.mode,label=label or Path(source).name,transform=transform or 'identity')).replace('<','\\u003c')
        script=Path(__file__).with_name('cryosparc_density.js').read_text(encoding='utf-8')
        return PAGE.replace('__CONFIG__',config).replace('</html>','<script>'+script+'</script></html>')

    @app.post('/api/density/import')
    async def import_density(particles: UploadFile=File(...), passthrough: UploadFile|None=File(None)):
        if store.ignore_data:
            raise HTTPException(409,'Enable Data loading in the launcher to match particle exposures. Data MRCs are not needed.')
        if store.scan_state['status'] in ('new','indexing'):
            raise HTTPException(409,'Wait for the EPU index to finish before importing particles.')
        folder=Path(tempfile.mkdtemp(prefix='particle-import-',dir=store.root))
        async def save(upload,name):
            if not upload.filename.lower().endswith('.cs'):
                raise HTTPException(400,'Choose a .cs particle dataset.')
            path=folder/name;size=0
            with path.open('wb') as output:
                while chunk:=await upload.read(1024*1024):
                    size+=len(chunk)
                    if size>256*1024*1024:
                        raise HTTPException(413,'Particle importer limit: 256 MiB per .cs file.')
                    output.write(chunk)
            return path
        try:
            main=await save(particles,'particles.cs')
            extra=await save(passthrough,'passthrough.cs') if passthrough and passthrough.filename else None
        except Exception:
            shutil.rmtree(folder);raise
        def work():
            try:
                retained=store.root/'particle-sources'/uuid.uuid4().hex
                retained.mkdir(parents=True)
                saved_main=retained/'particles.cs';shutil.move(main,saved_main)
                saved_extra=None
                if extra:
                    saved_extra=retained/'passthrough.cs';shutil.move(extra,saved_extra)
                with density.lock:
                    previous=Path(density.sources.get('folder','')) if density.sources.get('folder') else None
                result=density.import_files(saved_main,saved_extra,dict(folder=str(retained),original_main=particles.filename,original_passthrough=passthrough.filename if passthrough else ''))
                result['file']=particles.filename
                if previous and previous != retained and previous.parent == store.root/'particle-sources':
                    shutil.rmtree(previous,ignore_errors=True)
                return result
            except Exception:
                if 'retained' in locals():shutil.rmtree(retained,ignore_errors=True)
                raise
            finally:
                shutil.rmtree(folder,ignore_errors=True)
        return dict(job=store.submit('Importing particle density',work))

    @app.post('/api/density/grid/{gid}')
    def grid_density(gid: str):
        store.grid(gid)
        return dict(job=store.submit('Mapping particle density',lambda:density.grid_density(gid,transform)))

    @app.get('/api/density/summary')
    def summary():
        return density.summary

    @app.post('/api/native-dialog')
    def native_dialog(value: dict):
        kind=value.get('kind','cs')
        if kind not in ('cs','folder'):raise HTTPException(400,'Invalid chooser type')
        try:path=choose_path(kind,value.get('prompt',''),value.get('initial') or None)
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
            raise HTTPException(500,str(exc)) from exc
        return dict(cancelled=path is None,path=str(path) if path else '',name=path.name if path else '')

    @app.post('/api/density/inspect-path')
    def inspect_density_path(value: dict):
        required={'location/micrograph_path'} if value.get('purpose')=='filter' else None
        try:return inspect_cs(value.get('path',''),bool(value.get('suggest_passthrough')),required)
        except (OSError,ValueError) as exc:raise HTTPException(400,str(exc)) from exc

    @app.post('/api/density/import-path')
    def import_density_path(value: dict):
        if store.ignore_data:
            raise HTTPException(409,'Enable Data loading in the launcher to match particle exposures.')
        if store.scan_state['status'] in ('new','indexing'):
            raise HTTPException(409,'Wait for the EPU index to finish before importing particles.')
        def checked(raw,required=True):
            if not raw and not required:return None
            path=Path(str(raw or '')).expanduser()
            if not path.is_absolute():raise HTTPException(400,'Use an absolute local path to each .cs file.')
            try:path=path.resolve(strict=True)
            except OSError as exc:raise HTTPException(404,'Particle file not found: '+str(path)) from exc
            if not path.is_file() or path.suffix.lower()!='.cs':raise HTTPException(400,'Choose a local .cs file.')
            return path
        main=checked(value.get('particles'));extra=checked(value.get('passthrough'),False)
        def work():
            with density.lock:
                previous=Path(density.sources.get('folder','')) if density.sources.get('folder') else None
            result=density.import_files(main,extra,dict(original_main=main.name,original_passthrough=extra.name if extra else ''))
            result['file']=main.name
            if previous and previous.parent==store.root/'particle-sources':shutil.rmtree(previous,ignore_errors=True)
            return result
        return dict(job=store.submit('Indexing particle density from local files',work))

    @app.post('/api/density/clear')
    def clear():
        with density.lock:
            folder=Path(density.sources.get('folder','')) if density.sources.get('folder') else None
            density.records={};density.summary={'loaded':False};density.sources={};density.version+=1
        if folder and folder.parent == store.root/'particle-sources':shutil.rmtree(folder,ignore_errors=True)
        return density.summary

    from hole_selection_app import install_hole_selection
    install_hole_selection(app,store,density,transform)

    return app

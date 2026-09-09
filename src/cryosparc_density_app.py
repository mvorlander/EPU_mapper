"""Optional particle-density routes for the acquisition dashboard."""
import json
from pathlib import Path
import shutil
import tempfile
from fastapi import File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse
from acquisition_ui import PAGE
from cryosparc_density import DensityIndex


def install_density(app, source, label=None, transform='identity'):
    store=app.state.acquisition_store
    density=DensityIndex(store);app.state.density=density
    app.router.routes[:]=[r for r in app.router.routes if getattr(r,'path',None)!='/']

    @app.get('/',response_class=HTMLResponse)
    def home():
        config=json.dumps(dict(mode='acquisition',label=label or Path(source).name,transform=transform or 'identity')).replace('<','\\u003c')
        script=Path(__file__).with_name('cryosparc_density.js').read_text(encoding='utf-8')
        return PAGE.replace('__CONFIG__',config).replace('</html>','<script>'+script+'</script></html>')

    @app.post('/api/density/import')
    async def import_density(particles: UploadFile=File(...), passthrough: UploadFile|None=File(None)):
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
                result=density.import_files(main,extra)
                result['file']=particles.filename
                return result
            finally:
                shutil.rmtree(folder)
        return dict(job=store.submit('Importing particle density',work))

    @app.post('/api/density/grid/{gid}')
    def grid_density(gid: str):
        store.grid(gid)
        return dict(job=store.submit('Mapping particle density',lambda:density.grid_density(gid,transform)))

    @app.get('/api/density/summary')
    def summary():
        return density.summary

    @app.post('/api/density/clear')
    def clear():
        with density.lock:
            density.records={};density.summary={'loaded':False};density.version+=1
        return density.summary

    return app

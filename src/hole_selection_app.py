"""Routes and dashboard assets for EPU hole filtering and brush selection."""
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from hole_selection import export_particles, intensity_histogram, save_selection, selection_document, selection_payload, session_filter


def install_hole_selection(app, store, density, transform='identity'):
    # Density installs the dashboard route immediately before this extension.
    homes=[r for r in app.router.routes if getattr(r,'path',None)=='/']
    if homes:
        old_endpoint=homes[-1].endpoint
        app.router.routes.remove(homes[-1])

        @app.get('/',response_class=HTMLResponse)
        def home():
            response=old_endpoint()
            page=response.body.decode() if hasattr(response,'body') else str(response)
            script=Path(__file__).with_name('hole_selection.js').read_text(encoding='utf-8')
            return page.replace('</html>','<script>'+script+'</script></html>')

    @app.get('/api/hole-selection/session')
    def filter_settings():
        return session_filter(store)

    @app.get('/api/hole-selection/grid/{gid}')
    def get_selection(gid: str):
        store.grid(gid)
        return selection_payload(store,gid,transform)

    @app.post('/api/hole-selection/grid/{gid}')
    def set_selection(gid: str,value: dict):
        try:
            return save_selection(store,gid,value,transform)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post('/api/hole-selection/histogram')
    def histogram():
        return dict(job=store.submit('Building EPU intensity histogram',lambda:intensity_histogram(store,transform)))

    @app.get('/api/hole-selection/file')
    def selection_file():
        name=Path(str(getattr(store,'source','EPU_session'))).name or 'EPU_session'
        safe=''.join(c if c.isalnum() or c in '-_' else '_' for c in name)
        return JSONResponse(selection_document(store,transform),headers={
            'Content-Disposition':f'attachment; filename="{safe}_FoilHole_selection.epuholes.json"'})

    @app.post('/api/hole-selection/export')
    def start_export(value: dict):
        keep_unmatched=bool(value.get('keep_unmatched',False))
        include_excluded=bool(value.get('include_excluded',False))
        output=value.get('output_directory') or None
        if output:
            output=Path(str(output)).expanduser()
            if not output.is_absolute():raise HTTPException(400,'Use an absolute output-folder path.')
            try:output=output.resolve(strict=True)
            except OSError as exc:raise HTTPException(404,'Output folder not found: '+str(output)) from exc
            if not output.is_dir():raise HTTPException(400,'The export destination must be a folder.')
        def cs_path(raw,required=False):
            if not raw:
                if required:raise HTTPException(400,'Choose a CryoSPARC particle file.')
                return None
            path=Path(str(raw)).expanduser()
            if not path.is_absolute():raise HTTPException(400,'Use an absolute path to each .cs file.')
            try:path=path.resolve(strict=True)
            except OSError as exc:raise HTTPException(404,'CryoSPARC file not found: '+str(path)) from exc
            if not path.is_file() or path.suffix.lower()!='.cs':raise HTTPException(400,'Choose a CryoSPARC .cs file.')
            return path
        main=cs_path(value.get('particles'));passthrough=cs_path(value.get('passthrough'))
        if passthrough and not main:raise HTTPException(400,'Choose the particle table before its passthrough.')
        sources=dict(main=str(main),passthrough=str(passthrough) if passthrough else '') if main else None
        return dict(job=store.submit('Filtering CryoSPARC particles',lambda:export_particles(
            store,density,transform,keep_unmatched,include_excluded,output,sources)))

    @app.get('/api/hole-selection/export/{key}')
    def download_export(key: str):
        if not key.isalnum():raise HTTPException(400,'Invalid export key')
        path=store.root/'particle-exports'/(key+'.zip')
        if not path.is_file():raise HTTPException(404,'Export not found')
        return FileResponse(path,media_type='application/zip',filename='EPU_Mapper_filtered_particles.zip')

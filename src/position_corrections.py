"""User-observed targeting offsets; never alter microscope coordinates."""
import json
import math
import time
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response


def install_position_corrections(app, store, transform='identity'):
    @app.get('/position_corrections.js')
    def javascript():
        return FileResponse(Path(__file__).with_name('position_corrections.js'),media_type='text/javascript')

    def records(gid=None):
        rows=store.annotation_snapshot()
        return [v for k,v in rows.items() if k.startswith('position:') and (gid is None or v['grid_id']==gid)]

    @app.get('/api/position-corrections.json')
    def export():
        return Response(json.dumps(dict(source=str(store.source),coordinate_convention='Normalized GridSquare image coordinates: x right, y down. Arrow: EPU → observed. Not stage-space measurements.',corrections=records()),indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="observed-targeting-offsets.json"'})

    @app.get('/api/position-corrections/{gid}')
    def get(gid:str):
        store.grid(gid)
        return dict(corrections=records(gid))

    @app.post('/api/position-corrections/{gid}/{hole}')
    def save(gid:str,hole:str,value:dict):
        grid=store.grid(gid)
        foil_id=value.get('foil','')
        foil=store.media(foil_id)
        if foil['kind']!='foil' or foil['grid_id']!=gid or str(foil['hole'])!=hole:
            raise HTTPException(400,'Select a FoilHole with its own preview in this GridSquare.')
        key=f'position:{gid}:{hole}:{foil_id}'
        if value.get('remove') is True:
            store.execute('DELETE FROM annotations WHERE key=?',(key,))
            return dict(removed=True)
        try:
            x,y=float(value['x']),float(value['y'])
            if not all(math.isfinite(v) and 0<=v<=1 for v in (x,y)):
                raise ValueError()
        except (KeyError,TypeError,ValueError):
            raise HTTPException(400,'Choose a point inside the GridSquare image.')
        marker=next((m for m in json.loads(grid['markers']) if str(m['hole'])==hole),None)
        if not marker:
            raise HTTPException(409,'Wait for FoilHole mapping before marking an observed position.')
        clean=dict(grid_id=gid,grid_name=grid['name'],grid_image=grid['image'],hole=hole,
                   foil=foil_id,foil_name=foil['name'],foil_acquisition_stamp=foil['stamp'],original=[marker['x'],marker['y']],observed=[x,y],
                   dx=x-marker['x'],dy=y-marker['y'],transform=transform or 'identity',updated=time.time(),
                   kind='user_observed_position',note=str(value.get('note',''))[:10000])
        store.execute('INSERT OR REPLACE INTO annotations VALUES (?,?)',(key,json.dumps(clean)))
        return clean

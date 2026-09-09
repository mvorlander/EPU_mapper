import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from acquisition_app import create_acquisition_app
from fastapi.testclient import TestClient


class PositionTests(unittest.TestCase):
    def test_save_replace_export_and_remove_without_changing_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=create_acquisition_app(tmp,mode='foilhole',cache_root=Path(tmp)/'cache')
            store=app.state.acquisition_store
            store.set_meta('indexed:foilhole',1)
            markers=json.dumps([dict(hole='1',x=.2,y=.3)])
            store.execute('INSERT INTO grids VALUES (?,?,?,?,?,?,?,?,?)',('g',tmp,'GridSquare_1','image','','',1,markers,''))
            store.execute('INSERT INTO media VALUES (?,?,?,?,?,?,?,?,?)',('f','g','foil','1','FoilHole_1.jpg','preview','','',''))
            store.execute('INSERT INTO media VALUES (?,?,?,?,?,?,?,?,?)',('d','g','data','2','Data.jpg','data','','',''))
            with TestClient(app) as c:
                payload=dict(foil='f',x=.4,y=.5,note='Visually checked')
                r=c.post('/api/position-corrections/g/1',json=payload)
                self.assertEqual(r.status_code,200,r.text)
                self.assertAlmostEqual(r.json()['dx'],.2)
                self.assertEqual(store.grid('g')['markers'],markers)
                self.assertEqual(c.post('/api/position-corrections/g/1',json={**payload,'x':1.1}).status_code,400)
                self.assertEqual(c.post('/api/position-corrections/g/2',json=payload).status_code,400)
                self.assertEqual(c.post('/api/position-corrections/g/1',json={**payload,'x':.6}).status_code,200)
                rows=c.get('/api/position-corrections/g').json()['corrections']
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['observed'],[.6,.5])
                self.assertEqual(len(c.get('/api/position-corrections.json').json()['corrections']),1)
                self.assertIn('position:g:1:f',c.get('/api/annotations.json').json()['annotations'])
                self.assertEqual(c.get('/position_corrections.js').status_code,200)
                self.assertEqual(c.post('/api/position-corrections/g/1',json={'foil':'f','remove':True}).status_code,200)
                self.assertEqual(c.get('/api/position-corrections/g').json()['corrections'],[])

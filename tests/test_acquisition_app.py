import contextlib
import io
from pathlib import Path
import sys
import tempfile
import time
import subprocess
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from acquisition_app import create_acquisition_app
from scripts.simulate_acquisition import generate
from scripts.windows_gui_launcher import _review_command


class AcquisitionAppTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            self.source,self.atlas=generate(self.root/'fixture',images=9,squares=1,holes_per_square=2,grid_mrc=True)

    def tearDown(self):
        self.temp.cleanup()

    def app(self,mode='acquisition'):
        return create_acquisition_app(self.source,self.atlas,mode,cache_root=self.root/'cache')

    def ready(self,client):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            status=client.get('/api/status').json()
            if status['index']['status'] not in ('new','indexing'):
                self.assertNotEqual(status['index']['status'],'error',status)
                return status
            time.sleep(.01)
        self.fail('Index did not finish')

    def job(self,client,response):
        self.assertEqual(response.status_code,200,response.text)
        value=response.json()
        if 'job' not in value:return value
        # The first geometry request imports plotting/calibration modules.
        # Allow cold-start time instead of a brittle ~2-second poll budget.
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            result=client.get('/api/jobs/'+value['job']).json()
            if result['status']=='done':return result.get('result')
            self.assertNotEqual(result['status'],'error',result)
            time.sleep(.01)
        self.fail('Job did not finish: '+str(result))

    def test_acquisition_images_geometry_mrc_and_annotations(self):
        with TestClient(self.app()) as client:
            self.assertIn('Acquisition',client.get('/').text)
            status=self.ready(client);grid=status['grids'][0]
            self.assertEqual(grid['exposures'],9)
            self.assertIsInstance(grid['markers'],list)
            holes=client.get('/api/holes/'+grid['id']).json()['rows']
            rows=client.get('/api/exposures/'+grid['id']+'/'+holes[0]['hole']).json()['exposures']
            self.assertEqual(len(rows),4)
            geometry=self.job(client,client.post('/api/geometry/'+grid['id']))
            self.assertEqual(len(geometry['markers']),2)
            self.assertEqual(geometry['markers'][0]['x'],100/1024)
            for key in (grid['image'],grid['mrc'],rows[0]['id'],rows[0]['foil']):
                self.job(client,client.post('/api/prepare/'+key,json={'viewer':'data'}))
                image=client.get('/api/image/'+key)
                self.assertEqual(image.status_code,200,image.text[:80] if image.status_code!=200 else '')
                from PIL import Image
                with Image.open(io.BytesIO(image.content)) as im:im.load()
            self.assertEqual(client.get('/api/image/'+rows[0]['id']+'?adjust=true&low=99&high=1').status_code,400)
            key='grid:'+grid['id']
            self.assertEqual(client.post('/api/annotation/'+key,json={'comment':'Keep','rating':4,'status':'suitable'}).status_code,200)
            self.assertEqual(client.get('/api/annotation/'+key).json()['comment'],'Keep')
            self.assertIn(key,client.get('/api/annotations.json').json()['annotations'])
            self.assertEqual(client.post('/api/annotation/grid:missing',json={}).status_code,404)

    def test_foilhole_mode_does_not_scan_or_serve_data(self):
        app=self.app('foilhole');store=app.state.acquisition_store
        original=store.list_names
        def guarded(path,**kwargs):
            self.assertNotEqual(Path(path).name,'Data')
            return original(path,**kwargs)
        store.list_names=guarded
        with TestClient(app) as client:
            status=self.ready(client);grid=status['grids'][0]
            self.assertIsNone(grid['exposures'])
            holes=client.get('/api/holes/'+grid['id']).json()['rows']
            pair=client.get('/api/exposures/'+grid['id']+'/'+holes[0]['hole']).json()
            self.assertEqual(pair['exposures'],[])
            self.assertTrue(pair['foil'])
            forbidden=store.add_media(self.source/'Data/should_not_read.mrc','data')
            self.assertEqual(client.post('/api/prepare/'+forbidden,json={}).status_code,404)

    def test_launcher_command_selects_mode(self):
        for mode in ('screening','acquisition','foilhole'):
            command=_review_command(str(self.source),'127.0.0.1','8000',str(self.atlas),True,True,False,'identity',review_mode=mode)
            self.assertEqual(command[command.index('--mode')+1],mode)

    def test_stale_annotation_save_cannot_enable_new_form(self):
        from acquisition_ui import PAGE
        invalidate=PAGE.split('function invalidateAnnotation()',1)[1].split('function formDisabled',1)[0]
        disable=PAGE.split('function formDisabled(value)',1)[1].split('formDisabled(true);let dirty',1)[0]
        save=PAGE.split('async function saveIfDirty()',1)[1].split('\n',1)[0]
        script="""const assert=require('node:assert/strict');
const fields={};function $(id){return fields[id]||(fields[id]={disabled:false,value:'',checked:false,textContent:''})}
let annotationToken=1,annotationKey='grid:old',dirty=true,grids=[],resolve;
function api(){return new Promise(r=>resolve=r)}function renderAtlas(){}function fail(e){throw e}
"""+'function invalidateAnnotation()'+invalidate+'\nfunction formDisabled(value)'+disable+'\nasync function saveIfDirty()'+save+"""
(async()=>{const pending=saveIfDirty();invalidateAnnotation();resolve({});await pending;
assert.equal($('comment').disabled,true);assert.equal(annotationKey,'');
assert.notEqual($('saved').textContent,'Saved locally');})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result=subprocess.run(['node'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_dispatch_does_not_call_screening_scan(self):
        import review_app
        with patch.object(review_app,'_collect_grids',side_effect=AssertionError('Screening scan invoked')):
            app=review_app.create_app(self.source,review_mode='foilhole')
            self.assertEqual(app.state.acquisition_store.mode,'foilhole')
            app.state.acquisition_store.close()


if __name__=='__main__':unittest.main()

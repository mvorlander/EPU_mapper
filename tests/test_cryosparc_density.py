import sys
import tempfile
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/cryosparc_density'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from cryosparc_density import DensityIndex, exposure_key, load_locations
from cryosparc_density_app import inspect_cs


class DensityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.key='FoilHole_12_Data_34_2_20260904_123456'

    def tearDown(self):self.tmp.cleanup()

    def save(self,name,arr):
        path=self.root/name
        with path.open('wb') as f:np.save(f,arr,allow_pickle=False)
        return path

    def locations(self):
        a=np.zeros(2,dtype=[('uid','u8'),('location/micrograph_path','S200'),('location/center_x_frac','f4'),('location/center_y_frac','f4')])
        a['uid']=[2,1];a['location/micrograph_path']=(self.key+'_EER_patch_aligned_denoised.mrc').encode()
        a['location/center_x_frac']=[.8,.2];a['location/center_y_frac']=[.9,.1]
        return a

    def test_uid_join_and_y_flip(self):
        main=np.array([(1,),(2,)],dtype=[('uid','u8')])
        m=self.save('main.cs',main);p=self.save('pass.cs',self.locations())
        fields,n=load_locations(m,p)
        self.assertEqual(n,2);self.assertAlmostEqual(fields['location/center_x_frac'][0],.2)
        class Store:
            def execute(s,q):return [dict(id='m',grid_id='g',hole='12',name=self.key+'.jpg')]
        index=DensityIndex(Store());summary=index.import_files(m,p)
        self.assertEqual(summary['matched'],2)
        self.assertEqual(summary['grid_counts'],{'g':2})
        hist=np.asarray(index.records['m']['histogram'])
        self.assertEqual(hist[14,3],1);self.assertEqual(hist[1,12],1)

    def test_bad_passthrough_and_duplicate_uids(self):
        main=self.save('main.cs',np.array([(7,)],dtype=[('uid','u8')]))
        with self.assertRaises(ValueError):load_locations(main,self.save('pass.cs',self.locations()))
        data=self.locations();data['uid']=[1,1]
        with self.assertRaises(ValueError):load_locations(self.save('dups.cs',data))

    def test_header_inspection_suggests_unique_passthrough(self):
        main=self.save('particles_selected.cs',np.array([(1,),(2,)],dtype=[('uid','u8')]))
        passthrough=self.save('J53_passthrough_particles_selected.cs',self.locations())
        result=inspect_cs(main,True)
        self.assertTrue(result['needs_passthrough'])
        self.assertEqual(result['particles'],2)
        self.assertEqual(result['suggestion'],str(passthrough.resolve()))
        self.assertIn('location/micrograph_path',inspect_cs(passthrough)['fields'])

    def test_identity_invalid_and_ambiguous(self):
        self.assertEqual(exposure_key('J8/'+self.key+'_EER_123_patch.mrc'),self.key)
        self.assertNotEqual(exposure_key(self.key+'.jpg'),exposure_key(self.key.replace('_2_','_3_')+'.jpg'))
        class Store:
            def execute(s,q):return [dict(id=str(i),grid_id=str(i),hole='12',name=self.key+'.jpg') for i in range(2)]
        data=self.locations();data['location/center_x_frac'][0]=np.nan
        index=DensityIndex(Store());r=index.import_files(self.save('p.cs',data))
        self.assertEqual((r['invalid'],r['ambiguous'],r['matched']),(1,1,0))

    def test_projection_and_area_normalization(self):
        from unittest.mock import patch
        class Store:
            def acquisition_areas(s,gid):return {'areas':[dict(id='m',points=[(0,0),(.5,0),(.5,.5),(0,.5)])]}
            def grid(s,gid):return {'image':'g'}
            def media(s,key):return {'xml':'x'}
            def cache_optional(s,p):return p
        index=DensityIndex(Store())
        hist=np.zeros((16,16),dtype=int);hist[0,0]=2
        index.records={'m':dict(id='m',grid_id='g',hole='12',particles=2,histogram=hist.tolist())}
        info=dict(ref_matrix=(1e-8,0,0,1e-8),readout_width=100,readout_height=100)
        with patch('build_collage.parse_grid_info',return_value=info):r=index.grid_density('g')
        self.assertAlmostEqual(r['holes'][0]['density'],8)
        self.assertEqual(len(r['cells']),16)
        self.assertAlmostEqual(r['cells'][0]['density'],128)
        self.assertEqual(r['cells'][0]['points'][2],[.125,.125])

    def test_experimental_upload_route(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from app import create_density_app
        import time
        app=create_density_app(self.root,cache_root=self.root/'cache')
        store=app.state.acquisition_store
        store.set_meta('indexed:acquisition',1)
        store.scan_state['status']='cached'
        p=self.save('particles.cs',self.locations())
        store.execute('INSERT INTO media VALUES (?,?,?,?,?,?,?,?,?)',('m','g','data','12',self.key+'.jpg',str(self.root/(self.key+'.jpg')),'','',''))
        with TestClient(app) as client:
            response=client.post('/api/density/import',files={'particles':('particles.cs',p.read_bytes(),'application/octet-stream')})
            self.assertEqual(response.status_code,200,response.text)
            for _ in range(100):
                job=client.get('/api/jobs/'+response.json()['job']).json()
                if job['status'] in ('done','error'):break
                time.sleep(.01)
            self.assertEqual(job['status'],'done',job)
            self.assertEqual(job['result']['matched'],2)
            self.assertEqual(client.get('/api/density/summary').json()['grid_counts'],{'g':2})
            self.assertIn('CryoSPARC particle density',client.get('/').text)
            self.assertFalse(client.post('/api/density/clear').json()['loaded'])
            self.assertFalse(client.get('/api/density/summary').json()['loaded'])
            self.assertFalse(list(store.root.glob('particle-import-*')))

    def test_cached_session_loads_selected_atlas(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from app import create_density_app
        import time
        app=create_density_app(self.root,atlas=self.root/'atlas',cache_root=self.root/'cache')
        store=app.state.acquisition_store
        store.set_meta('indexed:acquisition',1)
        store.set_meta('atlas',{'id':'old'})
        def index(refresh=False):
            self.assertTrue(refresh)
            store.set_meta('atlas',{'id':'selected'})
        with patch.object(store,'_index_atlas',side_effect=index) as atlas_loader, patch.object(store,'start_scan') as scan:
            with TestClient(app) as client:
                for _ in range(100):
                    state=client.get('/api/status').json()
                    if state['index']['status']!='indexing':break
                    time.sleep(.01)
                self.assertEqual(state['atlas']['id'],'selected')
                atlas_loader.assert_called_once_with(refresh=True)
                scan.assert_not_called()


if __name__=='__main__':unittest.main()

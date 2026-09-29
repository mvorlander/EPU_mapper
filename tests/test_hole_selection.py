import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from hole_selection import export_particles, filter_particles_from_file, grid_targets, intensity_histogram, resolve_selection


class HoleSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def save_cs(self,name,array):
        path=self.root/name
        with path.open('wb') as handle:np.save(handle,array,allow_pickle=False)
        return path

    def test_epu_anchor_selection_and_manual_override(self):
        targets=[dict(hole='1',selected=True,primary=None,intensity=100),
                 dict(hole='2',selected=False,primary='1',intensity=200),
                 dict(hole='3',selected=False,primary=None,intensity=300)]
        selected=resolve_selection(targets,dict(mode='epu',low=None,high=None,overrides={'2':False,'3':True}))
        self.assertEqual(selected,{'1':True,'2':False,'3':True})
        selected=resolve_selection(targets,dict(mode='intensity',low=150,high=250,overrides={}))
        self.assertEqual(selected,{'1':False,'2':True,'3':False})

    def test_grid_metadata_intensity_parser(self):
        metadata=self.root/'GridSquare_7.dm'
        metadata.write_text('''<root><KeyValuePairOfintTargetLocationXmlBpEWF4JT><key>11</key><value><Id>11</Id><FilterProperties><PixelIntensityMean>123.5</PixelIntensityMean><PixelIntensityStDev>4.5</PixelIntensityStDev></FilterProperties><PixelCenter><x>25</x><y>75</y></PixelCenter><PrimaryId>9</PrimaryId><Selected>true</Selected><IsNearGridBar>false</IsNearGridBar><Quality>1</Quality></value></KeyValuePairOfintTargetLocationXmlBpEWF4JT></root>''')
        folder=self.root/'Images-Disc1'/'GridSquare_7';folder.mkdir(parents=True)
        class Store:
            def grid(s,gid):return {'path':str(folder),'image':'image'}
            def media(s,key):return {'xml':'grid.xml'}
            def cache_optional(s,path):return metadata if str(path).endswith('GridSquare_7.dm') else Path(path)
            def execute(s,query,args=()):return [dict(hole='11',exposures=3)]
        with patch('build_collage.parse_grid_info',return_value=dict(readout_width=100,readout_height=100)):
            result=grid_targets(Store(),'g')
        self.assertEqual(len(result['targets']),1)
        target=result['targets'][0]
        self.assertEqual((target['hole'],target['primary'],target['selected'],target['exposures']),('11','9',True,3))
        self.assertAlmostEqual(target['intensity'],123.5)
        self.assertAlmostEqual(target['x'],.25);self.assertAlmostEqual(target['y'],.75)

    def test_session_intensity_histogram_separates_epu_selected_counts(self):
        class Store:
            def grids(self):return [dict(id='g1'),dict(id='g2')]
        paths={}
        for gid,values in {'g1':[(10,True),(20,False)],'g2':[(30,True),(None,True)]}.items():
            items=''.join(f'<KeyValuePairOfintTargetLocationXmlBpEWF4JT><value><FilterProperties><PixelIntensityMean>{"" if value is None else value}</PixelIntensityMean></FilterProperties><Selected>{str(selected).lower()}</Selected></value></KeyValuePairOfintTargetLocationXmlBpEWF4JT>' for value,selected in values)
            paths[gid]=self.root/(gid+'.dm');paths[gid].write_text('<root>'+items+'</root>')
        with patch('hole_selection._grid_metadata',side_effect=lambda store,gid:paths[gid]):
            result=intensity_histogram(Store(),bins=2)
        self.assertEqual(result['total'],3)
        self.assertEqual(sum(result['counts']),3)
        self.assertEqual(sum(result['selected']),2)
        self.assertEqual((result['minimum'],result['maximum']),(10.0,30.0))

    def test_histogram_counts_beam_shift_members_with_selected_anchor(self):
        class Store:
            def grids(self):return [dict(id='g1')]
        metadata=self.root/'g1.dm'
        metadata.write_text('''<root>
          <KeyValuePairOfintTargetLocationXmlBpEWF4JT><key>1</key><value><Id>1</Id><FilterProperties><PixelIntensityMean>10</PixelIntensityMean></FilterProperties><Selected>true</Selected></value></KeyValuePairOfintTargetLocationXmlBpEWF4JT>
          <KeyValuePairOfintTargetLocationXmlBpEWF4JT><key>2</key><value><Id>2</Id><PrimaryId>1</PrimaryId><FilterProperties><PixelIntensityMean>20</PixelIntensityMean></FilterProperties><Selected>false</Selected></value></KeyValuePairOfintTargetLocationXmlBpEWF4JT>
          <KeyValuePairOfintTargetLocationXmlBpEWF4JT><key>3</key><value><Id>3</Id><FilterProperties><PixelIntensityMean>30</PixelIntensityMean></FilterProperties><Selected>false</Selected></value></KeyValuePairOfintTargetLocationXmlBpEWF4JT>
        </root>''')
        with patch('hole_selection._grid_metadata',return_value=metadata):
            result=intensity_histogram(Store(),bins=3)
        self.assertEqual(result['epu_selected'],2)
        self.assertEqual(sum(result['selected']),2)

    def test_particle_export_preserves_rows_uids_and_aligns_passthrough(self):
        key1='FoilHole_1_Data_10_1_20260904_120000'
        key2='FoilHole_2_Data_20_1_20260904_120001'
        main=np.array([(2,20),(1,10),(3,30)],dtype=[('uid','u8'),('value','i4')])
        passthrough=np.zeros(3,dtype=[('uid','u8'),('location/micrograph_path','S160'),('location/center_x_frac','f4'),('location/center_y_frac','f4')])
        passthrough['uid']=[3,2,1]
        passthrough['location/micrograph_path']=[b'unknown.mrc',(key1+'_EER.mrc').encode(),(key2+'_EER.mrc').encode()]
        passthrough['location/center_x_frac']=.5;passthrough['location/center_y_frac']=.5
        main_path=self.save_cs('main.cs',main);pass_path=self.save_cs('pass.cs',passthrough)
        class Store:
            root=self.root
            def execute(s,query,args=()):return [dict(id='a',grid_id='g',hole='1',name=key1+'.jpg'),dict(id='b',grid_id='g',hole='2',name=key2+'.jpg')]
            def grids(s):return [dict(id='g')]
            def annotation(s,key):return dict(mode='all',low=None,high=None,overrides={})
        class Density:
            lock=threading.RLock();sources=dict(main=str(main_path),passthrough=str(pass_path),original_main='selected.cs')
        targets=dict(targets=[dict(hole='1',selected=True,primary=None,intensity=1,exposures=1),dict(hole='2',selected=False,primary=None,intensity=2,exposures=1)],note='')
        with patch('hole_selection.grid_targets',return_value=targets),patch('hole_selection.session_filter',return_value={}):
            # Override hole 2 through the stored state while keeping hole 1.
            with patch.object(Store,'annotation',return_value=dict(mode='all',low=None,high=None,overrides={'2':False})):
                result=export_particles(Store(),Density(),include_excluded=True)
                direct=self.root/'direct';direct.mkdir()
                direct_result=export_particles(Store(),Density(),output_directory=direct)
                explicit=self.root/'explicit';explicit.mkdir()
                explicit_result=export_particles(Store(),None,output_directory=explicit,
                    sources=dict(main=str(main_path),passthrough=str(pass_path)))
        with zipfile.ZipFile(result['path']) as archive:
            with archive.open('particles_kept.cs') as handle:kept=np.load(handle,allow_pickle=False)
            with archive.open('particles_excluded.cs') as handle:excluded=np.load(handle,allow_pickle=False)
            with archive.open('passthrough_kept.cs') as handle:kept_pass=np.load(handle,allow_pickle=False)
            manifest=json.loads(archive.read('manifest.json'))
        self.assertEqual(kept['uid'].tolist(),[2])
        self.assertEqual(excluded['uid'].tolist(),[1,3])
        self.assertEqual(kept_pass['uid'].tolist(),[2])
        self.assertEqual(manifest['particles']['unmatched'],1)
        self.assertEqual(manifest['unmatched_policy'],'exclude')
        self.assertEqual(direct_result['key'],'')
        self.assertTrue((Path(direct_result['path'])/'particles_kept.cs').is_file())
        self.assertFalse((Path(direct_result['path'])/'particles_excluded.cs').exists())
        self.assertEqual(explicit_result['kept'],1)

    def test_portable_selection_filters_arbitrary_path_only_tables(self):
        key1='FoilHole_1_Data_10_1_20260904_120000';key2='FoilHole_2_Data_20_1_20260904_120001'
        main=self.save_cs('later_processing.cs',np.array([(20,2),(10,1),(30,3)],dtype=[('uid','u8'),('score','f4')]))
        passthrough=np.array([(30,b'unknown.mrc'),(10,(key1+'.mrc').encode()),(20,(key2+'.mrc').encode())],dtype=[('uid','u8'),('location/micrograph_path','S160')])
        pass_path=self.save_cs('later_passthrough.cs',passthrough)
        selection=dict(format='EPU Mapper FoilHole selection',version=1,source='test',grids={},
                       holes=[dict(grid_square='g',foil_hole='1',selected=True),dict(grid_square='g',foil_hole='2',selected=False)],
                       exposures=[dict(key=key1,grid_square='g',foil_hole='1',selected=True),dict(key=key2,grid_square='g',foil_hole='2',selected=False)])
        selection_path=self.root/'test.epuholes.json';selection_path.write_text(json.dumps(selection))
        output=self.root/'filtered';output.mkdir()
        result=filter_particles_from_file(selection_path,main,pass_path,output)
        kept=np.load(Path(result['path'])/'particles_kept.cs',allow_pickle=False)
        kept_pass=np.load(Path(result['path'])/'passthrough_kept.cs',allow_pickle=False)
        self.assertEqual(kept['uid'].tolist(),[10])
        self.assertEqual(kept_pass['uid'].tolist(),[10])
        self.assertEqual((result['matched'],result['unmatched'],result['kept']),(2,1,1))


if __name__=='__main__':unittest.main()

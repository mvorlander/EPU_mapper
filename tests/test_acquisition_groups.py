import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from acquisition_store import AcquisitionStore, matrix_vector, target_groups


class AcquisitionGroupTests(unittest.TestCase):
    def test_primary_group_and_rotated_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'grid.dm'
            path.write_text('<root><KeyValuePairOfintTargetLocationXmlBpEWF4JT><value><Id>2</Id><PrimaryId>1</PrimaryId></value></KeyValuePairOfintTargetLocationXmlBpEWF4JT></root>')
            self.assertEqual(target_groups(path),{'2':'1'})
        matrix=(2,3,4,7)
        self.assertEqual(matrix_vector(matrix,5,6),(34,57))
        self.assertEqual(matrix_vector(matrix,34,57,inverse=True),(5,6))
        with self.assertRaises(ValueError):matrix_vector((1,1,1,1),1,1,inverse=True)

    def test_planned_area_links_and_coordinate_scaling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            session=root/'EpuSession.dm'
            session.write_text('''<root><nested><TargetAreaTemplate>
              <PhysicalTransformation><matrix><_m11>2</_m11><_m12>0</_m12><_m21>0</_m21><_m22>2</_m22></matrix></PhysicalTransformation>
              <DataAcquisitionAreas><KeyValuePairOfintDataAcquisitionAreaXmlBpEWF4JT><value><Id>5</Id><ShiftInPixels><width>3</width><height>4</height></ShiftInPixels></value></KeyValuePairOfintDataAcquisitionAreaXmlBpEWF4JT></DataAcquisitionAreas>
              </TargetAreaTemplate></nested></root>''')
            store=AcquisitionStore(root,cache_root=root/'cache')
            try:
                row=dict(id='exposure',hole='2',name='FoilHole_2_Data_5_1_20260904_120000.jpg',xml='data.xml')
                def cached(path):
                    self.assertFalse(str(path).endswith('.mrc'))
                    return session if Path(path).name=='EpuSession.dm' else path
                def info(path):
                    return dict(readout_width=100 if path=='grid.xml' else 4,readout_height=100 if path=='grid.xml' else 4,ref_matrix=(1,0,0,1))
                with patch.object(store,'geometry',return_value={'markers':[dict(hole='2',anchor='1',x=.5,y=.5)]}),patch.object(store,'grid',return_value=dict(path=str(root/'Images-Disc1/GridSquare_1'),image='grid')),patch.object(store,'media',return_value={'xml':'grid.xml'}),patch.object(store,'cache_optional',side_effect=cached),patch.object(store,'execute',return_value=[row]),patch('build_collage.parse_grid_info',side_effect=info):
                    result=store.acquisition_areas('grid')
                    area=result['areas'][0]
                    self.assertEqual((area['hole'],area['anchor'],area['id']),('2','1','exposure'))
                    for actual,expected in zip(area['points'],[(.54,.56),(.58,.56),(.58,.60),(.54,.60)]):
                        self.assertAlmostEqual(actual[0],expected[0]);self.assertAlmostEqual(actual[1],expected[1])
                store.ignore_data=True
                with patch.object(store,'cache_optional',side_effect=AssertionError('No Data reads')):
                    self.assertEqual(store.acquisition_areas('grid')['areas'],[])
            finally:store.close()

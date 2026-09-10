"""Acquisition index checks independent of a real microscope session."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from acquisition_store import AcquisitionStore
from scripts.simulate_acquisition import generate


class AcquisitionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        with contextlib.redirect_stdout(io.StringIO()):
            self.session, self.atlas = generate(self.root / 'fixture', images=9, squares=1,
                                               holes_per_square=2, grid_mrc=False)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temporary.cleanup()

    def store(self, mode='acquisition'):
        store = AcquisitionStore(self.session, self.atlas, mode=mode, cache_root=self.root / 'cache')
        self.stores.append(store)
        return store

    def test_multiple_exposures_remain_grouped(self):
        store = self.store()
        store.scan()
        grid = store.grids()[0]
        self.assertEqual((grid['holes'], grid['exposures']), (2, 9))
        holes = store.holes(grid['id'])
        self.assertEqual([row['exposures'] for row in holes['rows']], [4, 5])
        ids = []
        for hole in holes['rows']:
            exposures = store.exposures(grid['id'], hole['hole'])['exposures']
            self.assertTrue(all(row['foil'] for row in exposures))
            self.assertEqual(len({row['foil'] for row in exposures}), 1)
            ids.extend(row['id'] for row in exposures)
        self.assertEqual(len(set(ids)), 9)

    def test_foilhole_mode_does_not_enter_data(self):
        store = self.store('foilhole')
        original = store.list_names
        def guarded(path, **kwargs):
            self.assertNotEqual(Path(path).name, 'Data')
            return original(path, **kwargs)
        store.list_names = guarded
        store.scan()
        self.assertEqual(store.grids()[0]['holes'], 2)
        self.assertIsNone(store.grids()[0]['exposures'])
        self.assertFalse(store.execute("SELECT * FROM media WHERE kind='data'"))

    def test_cached_image_and_annotation_survive_offline_reopen(self):
        store = self.store()
        store.scan()
        grid = store.grids()[0]
        expected = store.cache_file(grid['image']).read_bytes()
        store.annotate('grid:' + grid['id'], {'comment': 'Keep me', 'status': 'suitable'})
        self.stores.remove(store)
        store.close()
        original_stat = Path.stat
        def guarded(path, *args, **kwargs):
            if str(path).startswith(str(self.session)) or str(path).startswith(str(self.atlas)):
                raise OSError('Network unavailable')
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', guarded), patch('os.scandir', side_effect=OSError('Network unavailable')):
            reopened = self.store()
            self.assertEqual(reopened.cache_file(grid['image']).read_bytes(), expected)
            self.assertEqual(reopened.grids()[0]['annotation']['comment'], 'Keep me')

    def test_atlas_uses_xml_frame_not_maximum_screened_position(self):
        store = self.store()
        store.scan()
        atlas = store.meta('atlas')
        self.assertEqual((atlas['width'], atlas['height']), (1024, 1024))
        self.assertIn('900000', atlas['nodes'])

    def test_stitched_atlas_uses_mrc_header_without_reading_image_data(self):
        import mrcfile
        for extension in ('.mrc','.mrcs'):
            with self.subTest(extension=extension):
                path=self.atlas/('Atlas_SIMULATED'+extension)
                with mrcfile.new(path) as m:
                    m.header.nx=4005;m.header.ny=4005;m.header.nz=1
                # Deliberately header-only: reading the pixel payload would fail.
                self.assertEqual(path.stat().st_size,1024)
                store=self.store()
                with patch('mrcfile.open',wraps=mrcfile.open) as opened:
                    store._index_atlas()
                self.assertTrue(opened.call_args.kwargs['header_only'])
                atlas=store.meta('atlas')
                self.assertEqual((atlas['width'],atlas['height']),(4005,4005))
                self.assertEqual(atlas['frame_source'],'mrc-header')
                self.assertIn('900000',atlas['nodes'])
                self.assertFalse(store.local_file(atlas['mrc']))
                path.unlink()

    def test_stitched_atlas_without_mrc_does_not_use_camera_tile(self):
        path=self.atlas/'Atlas_SIMULATED.xml'
        xml=path.read_text()
        xml=xml.replace('</MicroscopeImage>','<CustomData><Entry><Key>NumberOfTilesAcquired</Key><Value>16</Value></Entry></CustomData></MicroscopeImage>')
        self.assertIn('NumberOfTilesAcquired',xml)
        path.write_text(xml)
        store=self.store();store._index_atlas()
        atlas=store.meta('atlas')
        self.assertEqual(atlas['nodes'],{})
        self.assertIsNone(atlas['width'])
        self.assertIn('assembled image dimensions',atlas['note'])

    def test_xml_overlays_without_metadata_or_data_reads(self):
        (self.session/'Metadata').rename(self.session/'Metadata-not-copied')
        store=self.store('foilhole')
        original=store.list_names
        def guarded(path,**kwargs):
            self.assertNotEqual(Path(path).name,'Data')
            return original(path,**kwargs)
        store.list_names=guarded
        store.scan()
        grid=store.grids()[0]
        original_cache=store.cache_file
        # Missing DM probes are metadata .dm files; allow them to fail without
        # pretending that image/MRC payloads are needed for the fallback.
        def metadata_only(key,**kwargs):
            self.assertIn(Path(store.media(key)['path']).suffix,('.xml','.dm'))
            return original_cache(key,**kwargs)
        store.cache_file=metadata_only
        result=store.geometry(grid['id'])
        self.assertEqual(result['xml_markers'],2)
        self.assertEqual(len(result['markers']),2)
        self.assertEqual(result['note'],'')
        first=next(m for m in result['markers'] if m['hole']=='100000000')
        self.assertAlmostEqual(first['x'],924/1024)
        self.assertAlmostEqual(first['y'],924/1024)
        with patch('os.scandir',side_effect=OSError('Offline')),patch.object(store,'cache_file',side_effect=AssertionError('Network read on cached geometry')):
            self.assertEqual(len(store.geometry(grid['id'])['markers']),2)

    def test_partial_xml_copy_refresh_adds_missing_hole(self):
        (self.session/'Metadata').rename(self.session/'Metadata-not-copied')
        foil_dir=self.session/'Images-Disc1/GridSquare_900000/FoilHoles'
        xml=sorted(foil_dir.glob('*.xml'))[1]
        pending=xml.with_suffix('.pending');xml.rename(pending)
        store=self.store('foilhole');store.scan();grid=store.grids()[0]
        first=store.geometry(grid['id'])
        self.assertEqual(len(first['markers']),1)
        self.assertIn('1 still lack',first['note'])
        pending.rename(xml)
        store.scan(refresh=True)
        self.assertEqual(len(store.geometry(grid['id'])['markers']),2)


if __name__ == '__main__':
    unittest.main()

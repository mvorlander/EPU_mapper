import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import mrcfile
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from acquisition_store import AcquisitionStore
from acquisition_ui import PAGE


class ImageScaleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=AcquisitionStore(self.root/'session',cache_root=self.root/'cache')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_detector_width_not_downsampled_preview_width(self):
        xml=self.root/'image.xml'
        xml.write_text('<root><pixelSize><x><numericValue>4e-10</numericValue></x></pixelSize><readoutArea><width>4096</width><height>4096</height></readoutArea></root>',encoding='utf-8')
        key=self.store.add_media(self.root/'image.jpg','data',xml=xml)
        self.assertAlmostEqual(self.store.image_calibration(key)['width_m'],4096*4e-10)
        # MRC and JPEG share the full field of view, irrespective of display resampling.
        key=self.store.add_media(self.root/'image.mrc','data_mrc',xml=xml)
        self.assertAlmostEqual(self.store.image_calibration(key)['width_m'],4096*4e-10)

    def test_stitched_atlas_uses_header_not_camera_tile(self):
        path=self.root/'atlas.mrc'
        with mrcfile.new(path) as m:
            m.set_data(np.zeros((40,60),dtype=np.float32));m.voxel_size=5000
        mrc=self.store.add_media(path,'atlas_mrc')
        image=self.store.add_media(self.root/'atlas.jpg','atlas')
        self.store.set_meta('atlas',dict(mrc=mrc,frame_source='mrc-header'))
        self.assertAlmostEqual(self.store.image_calibration(image)['width_m'],60*5000e-10)
        self.store.set_meta('atlas',dict(frame_source=None))
        self.assertIsNone(self.store.image_calibration(image)['width_m'])

    def test_missing_or_invalid_calibration_is_not_invented(self):
        key=self.store.add_media(self.root/'missing.jpg','foil')
        self.assertIsNone(self.store.image_calibration(key)['width_m'])
        xml=self.root/'invalid.xml';xml.write_text('<root><pixelSize><numericValue>nan</numericValue></pixelSize><readoutArea><width>512</width></readoutArea></root>',encoding='utf-8')
        key=self.store.add_media(self.root/'invalid.jpg','foil',xml=xml)
        self.assertIsNone(self.store.image_calibration(key)['width_m'])

    def test_filter_pagination_and_reversibility(self):
        for index in range(205):
            self.store.add_media(self.root/f'FoilHole_{index}.jpg','foil','g',str(index))
            if index%2==0:self.store.add_media(self.root/f'Data_{index}.jpg','data','g',str(index))
        self.assertEqual(self.store.holes('g')['total'],205)
        self.store.set_meta('review:exclude-empty-holes',True)
        first=self.store.holes('g');second=self.store.holes('g',100)
        self.assertEqual(first['total'],103)
        self.assertEqual(len(first['rows']),100);self.assertEqual(len(second['rows']),3)
        self.assertEqual(len(first['eligible_holes']),103)
        self.assertTrue(all(row['exposures'] for row in first['rows']+second['rows']))
        self.assertEqual(self.store.holes('g',exclude_empty=False)['total'],205)
        self.store.set_meta('review:exclude-empty-holes',False)
        self.assertEqual(self.store.holes('g')['total'],205)

    @unittest.skipUnless(shutil.which('node'),'Node required')
    def test_scale_units_zoom_and_custom_length(self):
        code='function scaleBarMetrics'+PAGE.split('function scaleBarMetrics',1)[1].split('const scaleStyle',1)[0]
        script=code+'''
const assert=require('node:assert/strict');
const initial=scaleBarMetrics(2e-6,500,500);
assert.equal(initial.label,'200 Å');assert.ok(Math.abs(initial.pixels-5)<1e-9);
assert.ok(Math.abs(scaleBarMetrics(2e-6,1000,500).pixels-10)<1e-9);
assert.ok(Math.abs(scaleBarMetrics(2e-6,500,500,400).pixels-10)<1e-9);
assert.equal(scaleBarMetrics(null,500,500),null);
assert.equal(scaleBarMetrics(2e-6,500,500,0).label,'200 nm');
'''
        result=subprocess.run(['node'],input=script,text=True,encoding='utf-8',capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

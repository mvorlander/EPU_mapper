"""Safety and physical-hole grouping checks for the network test fixture."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.simulate_acquisition import generate
from scripts.copy_simulated_acquisition import copy_fixture
from scripts.validate_simulated_acquisition import validate


class SimulationTests(unittest.TestCase):
    def test_structure_and_exclusive_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'SIMULATED'
            with contextlib.redirect_stdout(io.StringIO()):
                session, atlas = generate(target, images=9, squares=1, holes_per_square=2, grid_mrc=False)
            grid = session / 'Images-Disc1/GridSquare_900000'
            self.assertEqual(len(list((grid / 'Data').glob('*.jpg'))), 9)
            self.assertEqual(len(list((grid / 'Data').glob('*.xml'))), 9)
            self.assertEqual(len(list((grid / 'FoilHoles').glob('*.jpg'))), 2)
            self.assertFalse(list(target.rglob('*.mrc')))
            self.assertTrue((atlas / 'Atlas.dm').is_file())
            self.assertTrue(json.loads((target / 'SIMULATED_DATA.json').read_text())['simulated'])
            self.assertEqual(validate(target)['data_images'], 9)
            with self.assertRaises(FileExistsError):
                generate(target, images=1, squares=1, grid_mrc=False)

    def test_invalid_capacity_does_not_create_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'invalid'
            with self.assertRaises(ValueError):
                generate(target, images=10, squares=1, holes_per_square=1)
            self.assertFalse(target.exists())

    def test_copy_resumes_but_never_overwrites_different_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary) / 'source', Path(temporary) / 'target'
            with contextlib.redirect_stdout(io.StringIO()):
                generate(source, images=1, squares=1, grid_mrc=False)
                target.mkdir()
                (target / 'SIMULATED_DATA.json').write_bytes((source / 'SIMULATED_DATA.json').read_bytes())
                copy_fixture(source, target)
                copy_fixture(source, target)
            (target / 'README.txt').write_text('An unrelated edit that must be kept')
            with self.assertRaises(ValueError), contextlib.redirect_stdout(io.StringIO()):
                copy_fixture(source, target)
            self.assertEqual((target / 'README.txt').read_text(), 'An unrelated edit that must be kept')


if __name__ == '__main__':
    unittest.main()

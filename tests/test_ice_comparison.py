import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ice_comparison import join_exposures, read_epu_targets, summarise


class IceComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        metadata = self.root / "Metadata"; metadata.mkdir()
        (self.root / "EpuSession.dm").write_text('''<root><IceThicknessEnabled>false</IceThicknessEnabled><FilterHolesSettings><MinimumIntensity>100</MinimumIntensity><MaximumIntensity>200</MaximumIntensity></FilterHolesSettings></root>''')
        (metadata / "GridSquare_9.dm").write_text('''<root>
          <KeyValuePairOfintTargetLocationXmlBpEWF4JT><key>11</key><value><Id>11</Id><Selected>true</Selected><FilterProperties><PixelIntensityMean>150</PixelIntensityMean><PixelIntensityStDev>5</PixelIntensityStDev></FilterProperties></value></KeyValuePairOfintTargetLocationXmlBpEWF4JT>
          <KeyValuePairOfintTargetLocationXmlBpEWF4JT><key>12</key><value><Id>12</Id><PrimaryId>11</PrimaryId><Selected>false</Selected><FilterProperties><PixelIntensityMean>250</PixelIntensityMean></FilterProperties></value></KeyValuePairOfintTargetLocationXmlBpEWF4JT>
        </root>''')

    def tearDown(self):
        self.temp.cleanup()

    def save(self, name, data):
        path = self.root / name
        with path.open("wb") as handle: np.save(handle, data, allow_pickle=False)
        return path

    def test_namespace_independent_metadata_and_anchor_selection(self):
        targets, ambiguous, settings = read_epu_targets(self.root)
        self.assertFalse(ambiguous)
        self.assertTrue(targets["12"]["epu_selected"])
        self.assertEqual((settings["minimum"], settings["maximum"]), (100, 200))
        self.assertFalse(settings["ice_thickness_enabled"])

    def test_uid_aligned_passthrough_join_and_summary(self):
        main = np.array([(2, .8), (1, 1.2)], dtype=[("uid", "u8"), ("ctf_stats/ice_thickness_rel", "f4")])
        passthrough = np.array([(1, b"S1/import_movies/FoilHole_11_Data_1.eer"),
                                (2, b"S1/import_movies/FoilHole_12_Data_2.eer")],
                               dtype=[("uid", "u8"), ("movie_blob/path", "S100")])
        rows, unmatched, metadata = join_exposures(self.root, self.save("main.cs", main),
                                                   self.save("pass.cs", passthrough))
        self.assertFalse(unmatched)
        self.assertEqual([row["hole_id"] for row in rows], ["12", "11"])
        self.assertFalse(rows[0]["epu_in_recorded_range"])
        finite, holes, stats = summarise(rows)
        self.assertEqual((len(finite), len(holes)), (2, 2))
        self.assertAlmostEqual(stats["micrograph_raw_intensity_correlations"]["pearson"], -1.0)
        self.assertAlmostEqual(stats["micrograph_correlations"]["pearson"], 1.0)
        self.assertEqual(metadata["path_field"], "movie_blob/path")


if __name__ == "__main__": unittest.main()

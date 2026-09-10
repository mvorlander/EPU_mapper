import json
import base64
import io
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PIL import Image
from fastapi.testclient import TestClient
import build_collage as bc
import review_app
from collection_plan import build_plan, candidates, image_records, pair_media
from portable_session import export_portable_session, load_portable_session


class CollectionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="epumap-test-")
        self.root = Path(self.temp.name)
        self.base = self.root / "session"
        self.base.mkdir()
        self.grids = []
        for gid in (101, 102):
            grid = self.base / f"GridSquare_{gid}"
            grid.mkdir()
            (grid / "FoilHoles").mkdir()
            (grid / "Data").mkdir()
            self.make_image(grid / "GridSquare_20260827_120000.jpg")
            self.make_image(grid / "FoilHoles/FoilHole_11_20260827_120100.jpg")
            self.make_image(grid / "Data/FoilHole_11_Data_20260827_120200.jpg")
            self.make_image(grid / "Data/FoilHole_11_Data_20260827_120300.jpg")
            self.make_image(grid / "Data/FoilHole_99_Data_20260827_120400.jpg")
            self.grids.append(grid)
        self.responses = {g.name: dict(collection_status="suitable", rating=5-i, include=True, priority="primary" if i == 0 else "backup", comment="Evidence <not HTML> </script>") for i, g in enumerate(self.grids)}
        review_app._save_json_dict(self.base / "review_responses.json", self.responses)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def make_image(path):
        Image.new("RGB", (120, 80), "#658ca8").save(path)

    def client(self):
        return TestClient(review_app.create_app(self.base, None, None, False, None, "test", False, False, review_mode='legacy-screening'))

    def test_mrc_never_substitutes_different_acquisition(self):
        grid = self.grids[0]
        (grid / "GridSquare_20260828_120000.mrc").touch()
        self.assertIsNone(bc.find_grid_mrc(grid))
        match = grid / "GridSquare_20260827_120000.mrc"
        match.touch()
        self.assertEqual(bc.find_grid_mrc(grid), match)

    def test_png_deduplicates_equivalent_jpg(self):
        path = self.grids[0] / "Data/FoilHole_11_Data_20260827_120200.png"
        self.make_image(path)
        _, data = bc.gather_foil_and_data(self.grids[0])
        self.assertEqual(len(data["11"]), 2)
        self.assertIn(path, data["11"])

    def test_every_exposure_and_orphan_are_exported(self):
        records = list(image_records(self.grids[0], True))
        self.assertEqual(sum(data is not None for _, _, data in records), 3)
        self.assertTrue(any(fid == "99" and foil is None for fid, foil, _ in records))

    def test_pairs_use_acquisition_time_not_list_position(self):
        old = Path("FoilHole_11_20260827_120000.jpg")
        later = Path("FoilHole_11_20260827_130000.jpg")
        first = Path("FoilHole_11_Data_20260827_120100.jpg")
        repeat = Path("FoilHole_11_Data_20260827_120200.jpg")
        rows = list(pair_media({"11": [old, later]}, {"11": [first, repeat]}))
        self.assertEqual(rows[:2], [("11", old, first), ("11", old, repeat)])
        self.assertIn(("11", later, None), rows)

    def test_generated_javascript_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not available for JavaScript syntax checking")
        with self.client() as client:
            dashboard = client.get("/").text
        plan = build_plan(self.base, None, self.responses)
        for document in (dashboard, plan):
            for script in re.findall(r'<script>(.*?)</script>', document, re.S):
                check = subprocess.run([node, "--check"], input=script, text=True, encoding='utf-8', capture_output=True)
                self.assertEqual(check.returncode, 0, check.stderr)

    def test_pdf_all_exposures_and_readable_font(self):
        with patch.object(bc, "_draw_grid_summary_page", wraps=bc._draw_grid_summary_page) as render:
            bc.write_combined_report(self.base, self.root / "all.pdf", None, self.responses, all_screened_images=True)
        self.assertEqual(len(render.call_args_list), 2)
        self.assertEqual(sum(len(paths) for call in render.call_args_list for paths in call.args[4].values()), 6)
        font = bc._get_font(64)
        bbox = font.getbbox("Collection plan")
        self.assertGreater(bbox[3] - bbox[1], 25)

    def test_preferred_exposure_survives_compact_scope(self):
        preferred = "FoilHole_11_Data_20260827_120200.jpg"
        self.responses[self.grids[0].name]["preferred_hole"] = preferred
        document = build_plan(self.base, None, self.responses, collection_targets_only=True)
        payload = json.loads(re.search(r'id="plan-data">(.*?)</script>', document, re.S)[1])
        row = next(r for r in payload["records"] if r["key"] == "101")
        self.assertIn(preferred, [p["data_name"] for p in row["pairs"]])

    def test_scopes_and_priority(self):
        grids = bc._collect_grids(self.base)
        self.assertEqual(len(candidates(grids, self.responses, "representative")), 1)
        self.assertEqual(len(candidates(grids, self.responses, "targets")), 2)
        self.responses[self.grids[0].name]["include"] = False
        self.assertEqual(len(candidates(grids, self.responses, "targets")), 1)
        self.assertEqual(len(candidates(grids, self.responses, "all_screened")), 2)

    def test_html_embedded_safe_and_complete(self):
        html = build_plan(self.base, None, self.responses, all_screened_images=True)
        payload = json.loads(re.search(r'id="plan-data">(.*?)</script>', html, re.S)[1])
        self.assertEqual(len(payload["records"]), 2)
        self.assertEqual(sum(bool(p["data"]) for r in payload["records"] for p in r["pairs"]), 6)
        self.assertTrue(all(uri.startswith("data:image/jpeg;base64,") for uri in payload["assets"].values()))
        self.assertNotIn(str(self.base), html)
        self.assertIn("Evidence <not HTML> </script>", payload["records"][0]["comment"])
        self.assertNotIn('src="http', html)

    def test_html_uses_jpeg_for_atlas_and_never_reads_mrc(self):
        atlas = self.root / 'Atlas.jpg'
        self.make_image(atlas)
        mrc = self.grids[0] / 'GridSquare_20260827_120000.mrc'
        mrc.write_bytes(b'MRC originals must never be included in HTML')
        with patch('mrcfile.open', side_effect=AssertionError('HTML must not load MRC')):
            html = build_plan(self.base, str(atlas), self.responses, all_screened_images=True)
        payload = json.loads(re.search(r'id="plan-data">(.*?)</script>', html, re.S)[1])
        for uri in [payload['atlas'], payload['categories'], *payload['assets'].values()]:
            self.assertTrue(uri.startswith('data:image/jpeg;base64,'))
            with Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1]))) as image:
                self.assertEqual(image.format, 'JPEG')
        self.assertNotIn('data:image/png', html)
        self.assertNotIn('MRC originals must never', html)
        with patch.object(bc, '_load_image', side_effect=AssertionError('MRC must be skipped')):
            self.assertEqual(bc._embedded_path_uri(mrc), '')

    def test_jpeg_embedding_is_smaller_and_keeps_aspect_ratio(self):
        import numpy as np
        image = Image.fromarray(np.random.default_rng(1).integers(0, 256, (400, 600, 3), dtype=np.uint8))
        png = io.BytesIO()
        image.save(png, format='PNG')
        jpeg = base64.b64decode(bc._embedded_image_uri(image).split(',', 1)[1])
        self.assertLess(len(jpeg), len(png.getvalue()) * .65)
        small = base64.b64decode(bc._embedded_image_uri(image, max_size=300).split(',', 1)[1])
        self.assertEqual(Image.open(io.BytesIO(small)).size, (300, 200))

    def test_failed_save_keeps_confirmed_state(self):
        with self.client() as client:
            before = client.get("/grid_details?idx=0").json()
            with patch.object(review_app, "_save_json_dict", side_effect=OSError("disk full")):
                response = client.post("/review_state", json={"idx": 0, "comment": "must not appear"})
            self.assertEqual(response.status_code, 500)
            self.assertEqual(client.get("/grid_details?idx=0").json()["comment"], before["comment"])

    def test_review_priority_navigation_and_preview(self):
        with self.client() as client:
            response = client.post("/review_state", json={"idx": 0, "priority": "backup", "preferred_hole": "11", "comment": "Saved"})
            self.assertEqual(response.status_code, 200)
            details = client.get("/grid_details?idx=0").json()
            self.assertEqual(details["priority"], "backup")
            self.assertEqual(details["preferred_hole"], "11")
            self.assertEqual(details["data_count"], 3)
            self.assertEqual(len(details["holes"]), 3)
            self.assertEqual(client.get("/export_preview?scope=all_screened").json()["images"], 10)
            self.assertEqual(client.post("/target_order", json={"order": [1, 0]}).status_code, 200)
            self.assertEqual(client.post("/target_order", json={"order": [1, 1]}).status_code, 400)

    def test_html_job_download(self):
        with self.client() as client, patch.object(review_app, 'export_portable_session', side_effect=AssertionError('HTML must not copy a bundle')):
            response = client.post("/report_jobs", json={"kind": "html", "scope": "targets"})
            self.assertEqual(response.status_code, 200)
            jobid = response.json()["job_id"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                job = client.get(f"/report_jobs/{jobid}").json()
                if job["status"] in ("done", "error"):
                    break
                time.sleep(.05)
            self.assertEqual(job["status"], "done", job)
            download = client.get(job["download_url"])
            self.assertIn("text/html", download.headers["content-type"])
            self.assertIn("Collection plan", download.text)

    def test_html_finishes_while_bundle_copy_is_still_running(self):
        started, release = threading.Event(), threading.Event()
        def slow_copy(*args, **kwargs):
            started.set()
            if not release.wait(15):
                raise RuntimeError('Test copy was not released')
            return export_portable_session(*args, **kwargs)
        with self.client() as client, patch.object(review_app, 'export_portable_session', side_effect=slow_copy):
            copy = client.post('/portable_export', json={'destination': str(self.root)})
            self.assertEqual(copy.status_code, 200, copy.text)
            copy_id = copy.json()['job_id']
            try:
                self.assertTrue(started.wait(3))
                report = client.post('/report_jobs', json={'kind': 'html', 'scope': 'targets'})
                self.assertEqual(report.status_code, 200)
                report_id = report.json()['job_id']
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    state = client.get('/report_jobs/' + report_id).json()
                    if state['status'] in ('done', 'error'):
                        break
                    time.sleep(.05)
                self.assertEqual(state['status'], 'done', state)
                self.assertEqual(client.get('/portable_export/' + copy_id).json()['status'], 'running')
            finally:
                release.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    copy_state = client.get('/portable_export/' + copy_id).json()
                    if copy_state['status'] in ('done', 'error'):
                        break
                    time.sleep(.05)
            self.assertEqual(copy_state['status'], 'done', copy_state)

    def test_atomic_save_does_not_truncate_previous_file(self):
        path = self.base / "review_responses.json"
        before = path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("denied")):
            with self.assertRaises(OSError):
                review_app._save_json_dict(path, {"new": True})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_portable_archive_reopens_after_move(self):
        with patch.object(bc, 'write_embedded_html_report', side_effect=AssertionError('Bundle must not render HTML')):
            manifest = export_portable_session(self.base, None, "static", self.root, "test", {}, lambda _: None)
        self.assertFalse((manifest.parent / "Collection_plan.html").exists())
        self.assertNotIn('collection_plan', json.loads(manifest.read_text()))
        relocated = self.root / "relocated"
        manifest.parent.rename(relocated)
        loaded = load_portable_session(relocated / manifest.name)
        self.assertTrue(loaded["session_path"].is_dir())
        self.assertEqual(json.loads((loaded['session_path'] / 'review_responses.json').read_text()), self.responses)


if __name__ == "__main__":
    unittest.main()

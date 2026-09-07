import io
import json
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
import numpy as np
from PIL import Image
from fastapi.testclient import TestClient
from image_adjustments import adjust_array, adjusted_preview
from server_startup import reserve_socket, browser_url, ADDRESS_PREFIX, READY_PREFIX
from windows_gui_launcher import _consume_server_event, _start_browser_wait_page, _review_command
import review_app


class StartupTests(unittest.TestCase):
    def test_busy_port_fallback_is_reserved(self):
        with reserve_socket("127.0.0.1", 0) as occupied:
            port = occupied.getsockname()[1]
            with self.assertRaises(OSError):
                reserve_socket("127.0.0.1", port)
            with reserve_socket("127.0.0.1", port, auto_port=True) as fallback:
                actual = fallback.getsockname()[1]
                self.assertNotEqual(actual, port)
                with self.assertRaises(OSError):
                    reserve_socket("127.0.0.1", actual)
                self.assertGreater(occupied.fileno(), -1)

    def test_invalid_ports_and_ipv6_urls(self):
        for port in (-1, 65536):
            with self.assertRaises(ValueError):
                reserve_socket("127.0.0.1", port, True)
        self.assertEqual(browser_url("::", 8000), "http://[::1]:8000")
        self.assertEqual(browser_url("0.0.0.0", 8000), "http://127.0.0.1:8000")

    def test_readiness_requires_matching_child_event(self):
        state = {}
        payload = dict(port=8001, url="http://127.0.0.1:8001", requested_port=8000)
        self.assertIsNone(_consume_server_event("INFO: Application startup complete.", state))
        self.assertIsNone(_consume_server_event(READY_PREFIX + json.dumps(payload), state))
        self.assertEqual(_consume_server_event(ADDRESS_PREFIX + json.dumps(payload), state), "address")
        self.assertFalse(state["ready"])
        self.assertIn("8000 is already in use", state["notice"])
        self.assertIsNone(_consume_server_event(READY_PREFIX + json.dumps(dict(payload, port=8002)), state))
        self.assertEqual(_consume_server_event(READY_PREFIX + json.dumps(payload), state), "ready")
        self.assertTrue(state["ready"])
        for bad in ("null", "[]", "{}", "broken"):
            self.assertIsNone(_consume_server_event(ADDRESS_PREFIX + bad, state))

    def test_wait_page_does_not_connect_to_an_older_server(self):
        proc = Mock()
        proc.poll.return_value = None
        state = {}
        with reserve_socket("127.0.0.1", 0) as occupied:
            server, url = _start_browser_wait_page(proc, "127.0.0.1", str(occupied.getsockname()[1]), state)
            try:
                def poll():
                    with urlopen(url + "/ready", timeout=3) as response:
                        return json.load(response)
                self.assertFalse(poll()["ready"])
                state.update(ready=True, url="http://127.0.0.1:8001")
                self.assertEqual(poll()["url"], state["url"])
                self.assertTrue(poll()["ready"])
                proc.poll.return_value = 1
                self.assertFalse(poll()["ready"])
                self.assertIn("exit code 1", poll()["error"])
            finally:
                server.shutdown()
                server.server_close()

    def test_launcher_opts_into_automatic_port(self):
        command = _review_command("session", "127.0.0.1", "8000", "", True, False, False, "identity", auto_port=True)
        self.assertIn("--auto-port", command)

    def test_fixed_port_collision_fails_before_loading_images(self):
        with reserve_socket("127.0.0.1", 0) as occupied:
            with patch.object(sys, 'argv', ['review_app', 'session', '--port', str(occupied.getsockname()[1])]), \
                 patch.object(review_app, '_resolve_grid_root', return_value=Path('session')), \
                 patch.object(review_app, 'create_app') as create, \
                 patch.object(sys, 'stderr', io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as caught:
                    review_app.main()
                self.assertEqual(caught.exception.code, 2)
                create.assert_not_called()
                self.assertIn('No session images were loaded', errors.getvalue())

    def test_real_uvicorn_starts_on_reserved_fallback(self):
        code = '''
import sys
sys.path.insert(0, sys.argv[1])
from fastapi import FastAPI
from server_startup import reserve_socket, announce_address, run_reserved_server
app = FastAPI()
@app.get('/health')
def health(): return {'session': 'new-child'}
with reserve_socket('127.0.0.1', int(sys.argv[2]), True) as sock:
    address = announce_address('127.0.0.1', int(sys.argv[2]), sock)
    run_reserved_server(app, sock, address)
'''
        with reserve_socket("127.0.0.1", 0) as occupied:
            original = occupied.getsockname()[1]
            proc = subprocess.Popen([sys.executable, "-u", "-c", code, str(ROOT / "src"), str(original)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            lines = queue.Queue()
            thread = threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True)
            thread.start()
            state = {}
            try:
                while not state.get("ready"):
                    _consume_server_event(lines.get(timeout=15).strip(), state)
                self.assertNotEqual(state["port"], original)
                with urlopen(state["url"] + "/health", timeout=3) as response:
                    self.assertEqual(json.load(response), {"session": "new-child"})
                with self.assertRaises(OSError):
                    reserve_socket("127.0.0.1", original)
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                thread.join(timeout=2)
                proc.stdout.close()


class AdjustmentTests(unittest.TestCase):
    def test_dimensions_range_and_gamma(self):
        data = np.arange(600, dtype=np.float32).reshape(20, 30)
        original = data.copy()
        image = adjust_array(data, low=0, high=100)
        bright = adjust_array(data, low=0, high=100, gamma=2)
        self.assertEqual(image.size, (30, 20))
        self.assertEqual(image.getextrema(), (0, 255))
        self.assertGreater(np.asarray(bright).mean(), np.asarray(image).mean())
        np.testing.assert_array_equal(data, original)

    def test_low_pass_reduces_high_frequency_noise(self):
        y, x = np.indices((100, 100))
        data = x.astype(float) * 2 + 25 * (-1.) ** (x+y)
        raw = np.asarray(adjust_array(data, low=0, high=100), dtype=float)
        smooth = np.asarray(adjust_array(data, low=0, high=100, sigma=1.5), dtype=float)
        self.assertLess(np.abs(np.diff(smooth[10:-10, 10:-10], axis=0)).mean(), np.abs(np.diff(raw[10:-10, 10:-10], axis=0)).mean() / 10)

    def test_flat_nonfinite_and_rgb(self):
        for data in (np.full((8, 8), np.nan), np.ones((8, 8, 3))):
            image = adjust_array(data, sigma=1, mode="equalize")
            self.assertTrue(np.all(np.asarray(image) == 127))
        self.assertEqual(adjust_array(np.ones((20, 40)), max_size=10).size, (10, 5))

    def test_rejects_invalid_settings(self):
        for options in (dict(low=99, high=1), dict(low=float('nan')), dict(gamma=0), dict(sigma=5), dict(mode='bad')):
            with self.assertRaises(ValueError):
                adjust_array(np.zeros((8, 8)), **options)

    def test_png_mrc_and_api_are_nondestructive(self):
        import mrcfile
        with tempfile.TemporaryDirectory(prefix="epumap-adjust-test-") as temp:
            root = Path(temp)
            grid = root / "GridSquare_101"
            grid.mkdir()
            png = grid / "GridSquare_20260827_120000.png"
            Image.fromarray(np.arange(600, dtype=np.uint8).reshape(20, 30)).save(png)
            Image.open(png).save(png.with_suffix('.jpg'))
            mrc = png.with_suffix('.mrc')
            with mrcfile.new(mrc) as handle:
                handle.set_data(np.arange(600, dtype=np.float32).reshape(20, 30))
            before = [path.read_bytes() for path in (png, mrc)]
            for path in (png, mrc):
                self.assertEqual(adjusted_preview(path, sigma=1).size, (30, 20))
            with TestClient(review_app.create_app(root, None, None, False, None, "test", False, False)) as client:
                for mrc_mode in (False, True):
                    response = client.get('/adjusted_preview', params=dict(idx=0, kind='grid', mrc=mrc_mode, sigma=1, gamma=1.5))
                    self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else '')
                    self.assertEqual(Image.open(io.BytesIO(response.content)).size, (30, 20))
                self.assertEqual(client.get('/adjusted_preview?idx=0&kind=grid&low=90&high=10').status_code, 400)
                self.assertEqual(client.get('/adjusted_preview?idx=0&kind=unknown&mrc=true').status_code, 400)
                self.assertEqual(client.get('/adjusted_preview?idx=0&kind=data&name=../../other.png').status_code, 404)
            self.assertEqual(before, [path.read_bytes() for path in (png, mrc)])


if __name__ == '__main__':
    unittest.main()

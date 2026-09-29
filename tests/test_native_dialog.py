import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from native_dialog import _ps_literal, choose_path


class NativeDialogTests(unittest.TestCase):
    def test_powershell_literal_handles_spaces_quotes_and_dollar(self):
        self.assertEqual(_ps_literal("O'Brien $data"),"'O''Brien $data'")

    def test_choose_cs_normalizes_initial_file_to_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'particles.cs';source.write_bytes(b'test')
            with patch('native_dialog.sys.platform','darwin'),patch('native_dialog._mac_dialog',return_value=str(source)) as dialog:
                self.assertEqual(choose_path('cs','Choose particles',source),source.resolve())
            self.assertEqual(dialog.call_args.args[2],source.parent)

    def test_cancel_returns_none(self):
        with patch('native_dialog.sys.platform','darwin'),patch('native_dialog._mac_dialog',return_value=''):
            self.assertIsNone(choose_path('folder'))


if __name__=='__main__':unittest.main()

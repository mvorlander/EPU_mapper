# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['review_app', 'build_collage', 'scripts.plot_foilhole_positions', 'matplotlib.backends.backend_tkagg']
hiddenimports += collect_submodules('matplotlib')


a = Analysis(
    ['C:\\EPU_mapper\\EPU_mapper\\scripts\\windows_gui_launcher.py'],
    pathex=['C:\\EPU_mapper\\EPU_mapper\\src', 'C:\\EPU_mapper\\EPU_mapper'],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EPUMapperReview',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='EPUMapperReview',
)

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['review_app', 'build_collage', 'portable_session', 'collection_plan', 'dashboard_features', 'server_startup', 'image_adjustments', 'acquisition_store', 'acquisition_app', 'acquisition_ui', 'scripts.plot_foilhole_positions', 'matplotlib.backends.backend_tkagg']
hiddenimports += collect_submodules('matplotlib')
hiddenimports += ['cryosparc_density', 'cryosparc_density_app']
hiddenimports += ['hole_selection', 'hole_selection_app']
hiddenimports += ['native_dialog']
hiddenimports += ['position_corrections']


a = Analysis(
    ['C:\\EPU_mapper\\EPU_mapper\\scripts\\windows_gui_launcher.py'],
    pathex=['C:\\EPU_mapper\\EPU_mapper\\src', 'C:\\EPU_mapper\\EPU_mapper'],
    binaries=[],
    datas=[('src/cryosparc_density.js', '.'), ('src/hole_selection.js', '.'), ('src/position_corrections.js', '.')],
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

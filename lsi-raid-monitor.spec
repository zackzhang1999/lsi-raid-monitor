# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['bundle_entry.py'],
    pathex=[],
    binaries=[],
    datas=[('web', 'web'), ('lsi_report.py', '.'), ('lsi_collectd.py', '.'), ('storage_mgr.py', '.'), ('user_mgr.py', '.'), ('requirements.txt', '.')],
    hiddenimports=['flask', 'matplotlib', 'matplotlib.backends.backend_agg'],
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
    a.binaries,
    a.datas,
    [],
    name='lsi-raid-monitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

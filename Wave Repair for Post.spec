# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['wave_repair.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    name='Wave Repair for Post',
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
    name='Wave Repair for Post',
)
app = BUNDLE(
    coll,
    name='Wave Repair for Post.app',
    icon=None,
    bundle_identifier='com.hsuanice.wave-repair-for-post',
    # Lets Finder "Open With" / dropping on the Dock icon hand WAV files and
    # folders over — they arrive as open-document Apple Events, which the
    # app handles via ::tk::mac::OpenDocument (same path Wave Manager's
    # "Open in Wave Repair for Post" uses through `open -a`).
    info_plist={
        'CFBundleDocumentTypes': [
            {
                'CFBundleTypeName': 'WAV Audio',
                'CFBundleTypeRole': 'Viewer',
                'LSHandlerRank': 'Alternate',
                'LSItemContentTypes': ['com.microsoft.waveform-audio', 'public.folder'],
            },
        ],
    },
)

# vcnet-test.spec
# PyInstaller build spec for the vcnet-test binary.
#
# Build:
#   pip install pyinstaller
#   pyinstaller vcnet-test.spec
#
# Output: dist/vcnet-test  (single self-contained executable)
#
# Deploy alongside:
#   nettest.config.json   (vCenter credentials + settings)
#   ovf/                  (Alpine OVF template directory)
#   artifacts/            (created on first run)

from PyInstaller.utils.hooks import collect_submodules, collect_data_files
import sys as _sys

# ── Hidden imports ─────────────────────────────────────────────────────────────
# Keep hiddenimports minimal. PyInstaller will discover dependencies through:
# 1. Static analysis of imported modules in main.py
# 2. Automatic traversal of package dependencies
# Listing packages explicitly that PyInstaller can't find causes "not found" errors.
_uvloop = [] if _sys.platform == "win32" else ["uvloop"]

hiddenimports = (
    [
        "web_app",
        "nettest_runner",
    ]
    + _uvloop
)

# ── Data files ─────────────────────────────────────────────────────────────────
# Bundle static/ web assets so the UI works without external files.
# ovf/, artifacts/, and nettest.config.json are kept EXTERNAL.
datas = [
    ("static", "static"),
]

# Also pull in any data files from bundled packages
datas += collect_data_files("uvicorn")
datas += collect_data_files("fastapi")
datas += collect_data_files("starlette")

# ── Analysis ───────────────────────────────────────────────────────────────────
a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "PIL",
        "IPython",
        "jupyter",
        "test",
        "unittest",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # required for onedir: keep binaries out of EXE, let COLLECT handle them
    name="vcnet-test",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

# onedir mode: all deps live beside the executable.
# Startup is instant — no extraction needed on every launch.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="vcnet-test",
)

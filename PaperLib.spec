# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for PaperLib (Windows, one-folder / onedir build).

Build with:
    python -m PyInstaller PaperLib.spec --noconfirm

Produces  dist/PaperLib/PaperLib.exe  plus its support folder (_internal).

Notes:
- We use onedir (not onefile): more reliable for Tkinter + the NATIVE tkdnd
  binaries shipped by tkinterdnd2, and it starts faster.
- collect_all('tkinterdnd2') pulls in the platform tkdnd .dll / tcl files that
  drag-and-drop needs at runtime.
- collect_all('pypdfium2') bundles the native pdfium binary used to render PDF
  pages to images in the reader.
- collect_all('anthropic') is included to catch any package data files.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = []

# These ship native binaries / data files that must be bundled:
#   tkinterdnd2 -> tkdnd .dll + tcl (drag-and-drop)
#   pypdfium2   -> pdfium native library (PDF page rendering)
for pkg in ("tkinterdnd2", "pypdfium2", "anthropic"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Pillow's Tk image bridge is imported lazily, so name it explicitly.
hiddenimports += ["PIL.ImageTk"]

# Make sure every paperlib submodule is included.
hiddenimports += collect_submodules("paperlib")

block_cipher = None


a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PaperLib",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # windowed / no console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PaperLib",
)

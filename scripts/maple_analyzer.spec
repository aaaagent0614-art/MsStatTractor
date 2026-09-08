# PyInstaller spec for the MsStatTractor HUD.
#
# Build on Windows, from the repo root, inside the project venv:
#   .venv\Scripts\pyinstaller scripts\maple_analyzer.spec --noconfirm
#
# Output: dist\MsStatTractor\MsStatTractor.exe (one-folder build --
# faster startup than --onefile, and rapidocr's ONNX models are large enough
# that unpacking them to a temp dir on every launch isn't worth it).

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None
repo_root = Path(SPECPATH).resolve().parent

datas = []
binaries = []
hiddenimports = []

# Window/exe icon: the app.ico asset (Yeti-and-Wolf, user request 2026-09-08)
# is embedded into the exe (EXE icon= below) AND shipped next to the exe so
# the Tk windows can iconbitmap() it at runtime (_icon_path in overlay.py).
_ICON = repo_root / "assets" / "app.ico"
datas += [(_ICON.as_posix(), ".")]

for pkg in ("customtkinter", "rapidocr_onnxruntime", "windows_capture"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

# pywin32 modules are imported dynamically inside capture.py's
# GameWindowCapture.__init__ (they only exist on Windows), so list them
# explicitly rather than relying on PyInstaller's bytecode scan to spot the
# imports. (PrintWindow's GDI work uses ctypes, not win32ui, so there is no
# MFC/mfc140u.dll dependency to bundle.) windows_capture is likewise imported
# dynamically (inside _try_wgc_frame), so pin it as a hidden import too.
hiddenimports += ["win32gui", "win32api", "win32con", "win32process", "windows_capture"]

a = Analysis(
    [str(repo_root / "scripts" / "run_overlay.py")],
    pathex=[str(repo_root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["win32ui", "pythonwin"],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MsStatTractor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=_ICON.as_posix(),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MsStatTractor",
)

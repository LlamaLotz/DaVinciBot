# Build with: pyinstaller --noconfirm packaging/davincibot.spec
from pathlib import Path

root = Path(SPECPATH).parent

a = Analysis(
    [str(root / "src" / "davincibot" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[(str(root / "src" / "davincibot" / "resolve" / name), "davincibot/resolve")
           for name in ("runtime.py", "interchange.py")],
    hiddenimports=[
        "keyring.backends.Windows",
        "davincibot.resolve.launcher",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DaVinciBot",
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

from pathlib import Path


root = Path(SPECPATH)
analysis = Analysis(
    [str(root / "app" / "desktop.py")],
    pathex=[str(root)],
    datas=[(str(root / "static" / "index.html"), "static")],
    hiddenimports=[
        "uvicorn.lifespan.on",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.websockets_impl",
    ],
)

executable = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="LocalTorrent",
    console=True,
    icon=str(root / "assets" / "local-torrent.ico"),
    upx=False,
)

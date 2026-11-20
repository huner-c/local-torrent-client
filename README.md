# Local Torrent

Local Torrent is a Windows BitTorrent client with a browser interface. It uses Python, FastAPI, and libtorrent to verify pieces and write files directly to disk. Downloads continue when the browser tab is closed while the app is running.

## Run on Windows

Double-click `LocalTorrent.exe` to open the web interface in your default browser. The standalone build includes Python and its dependencies. Keep the executable in a writable folder. Local Torrent stores downloads and resume data next to the executable.

If you're running from a source checkout, install 64-bit Python 3.10 or later and double-click `start.cmd`. The script creates a virtual environment and installs the dependencies on first run. Open the interface after the server starts. [Open Local Torrent](http://127.0.0.1:8000/)

Press **Ctrl+C** in the app's console and wait for shutdown to finish so the app can save your progress. To change the executable's web port, run `LocalTorrent.exe --port 8001`.

## Add and manage downloads

1. Drop a `.torrent` file onto the upload area or paste a magnet link. The interface uses Turkish labels. **Torrent ekle** is the section for adding downloads.
2. Enter a custom folder, such as `D:\Downloads`, or use the default `downloads` folder. Each torrent gets a subfolder named after its info hash. The full path appears in the torrent list.
3. Check **Önce dosyaları seç** to choose files before the download starts. When metadata is available, open **Dosyalar** and apply your selection. Pieces that span multiple files may include bytes needed to verify a selected file. Previously downloaded files stay on disk when you change the selection.
4. Use **Duraklat** to pause or **Kaldır** to remove a torrent. The removal dialog lets you keep its files or delete them from disk.
5. Use **Hız limitleri** to set global download and upload limits. Enter `0` for unlimited. Actual speeds depend on peer availability, your connection, and disk performance.

## Storage and networking

- The app uses sparse allocation so large files do not reserve their full size up front. Use a file system that supports your target file sizes, such as NTFS, and leave enough free space to finish downloading. The 64 MiB upload limit applies to `.torrent` metadata.
- The app periodically writes checkpoints and saves them again on pause and clean shutdown. Metadata, resume state, and per-torrent settings live in `data/state/`. Global speed limits live in `data/settings.json`. Valid resume data lets libtorrent restore known pieces without a full hash scan. Missing or inconsistent resume data can trigger a disk check.
- The web interface listens on `127.0.0.1` only. The BitTorrent engine uses TCP and UDP port `6881` by default, with DHT, local peer discovery, UPnP, and NAT-PMP enabled. Completed torrents continue seeding until paused or removed.

## Build the executable

Run these commands in PowerShell from the source directory on Windows. Python 3.12 x64 is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt "pyinstaller>=6,<7"
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath . --workpath .\build LocalTorrent.spec
```

The build creates `LocalTorrent.exe` in the project folder. The icon is embedded from `assets/local-torrent.ico`. The executable and `start.cmd` use the same download and resume state when they run from the same folder. Run one instance per folder.

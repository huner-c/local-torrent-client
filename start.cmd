@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import fastapi, libtorrent, uvicorn, websockets, python_multipart" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
echo.
echo Local Torrent ^| http://127.0.0.1:8000
echo Press Ctrl+C to shut down safely.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo Local Torrent failed to start. Check the error above.
pause
exit /b 1

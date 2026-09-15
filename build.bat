@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :failed
.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed --name ProxyLauncher src\main.py
if errorlevel 1 goto :failed
echo.
echo Built dist\ProxyLauncher.exe
endlocal
exit /b 0

:failed
echo Build failed. Check the error above. No new EXE was confirmed.
endlocal
exit /b 1

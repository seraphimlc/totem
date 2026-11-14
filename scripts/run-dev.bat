@echo off
REM Start the local dev server on Windows. See also scripts\run-dev.ps1.
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo Create the venv first: powershell -ExecutionPolicy Bypass -File scripts\setup-venv.ps1 1>&2
  exit /b 1
)

if "%HOST%"=="" set HOST=127.0.0.1
if "%PORT%"=="" set PORT=8780

".venv\Scripts\python.exe" -m uvicorn app.main:app --host %HOST% --port %PORT% --reload
exit /b %ERRORLEVEL%

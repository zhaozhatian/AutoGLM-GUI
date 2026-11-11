@echo off
rem AutoGLM-GUI local service launcher.
rem Uses the fork's editable venv, ensures adb is on PATH, and enables the
rem coordinate grid by default.
setlocal
set "ROOT=%~dp0"
set "ADB_DIR=%USERPROFILE%\.cache\autoglm\platform-tools"

if not exist "%ADB_DIR%\adb.exe" (
  echo [AutoGLM] ADB not found, downloading Android platform-tools...
  "%ROOT%.venv\Scripts\python.exe" -c "from AutoGLM_GUI.adb_manager import ensure_adb; print(ensure_adb())"
  if errorlevel 1 (
    echo [AutoGLM] ADB download failed. Install platform-tools manually and add adb.exe to PATH.
    exit /b 1
  )
)

set "PATH=%ADB_DIR%;%PATH%"
set "AUTOGLM_ADB_PATH=%ADB_DIR%\adb.exe"
if not defined AUTOGLM_GRID_OVERLAY set "AUTOGLM_GRID_OVERLAY=1"
if not defined AUTOGLM_PORT set "AUTOGLM_PORT=8765"

cd /d "%ROOT%"
echo [AutoGLM] starting on http://127.0.0.1:%AUTOGLM_PORT%
"%ROOT%.venv\Scripts\autoglm-gui.exe" --no-browser --port %AUTOGLM_PORT% %*
if errorlevel 1 pause
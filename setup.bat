@echo off
REM setup.bat - Initialize dev environment (venv + deps + .env)
REM Run from cmd.exe:  setup.bat
REM Run from PowerShell (alternative):  cmd /c setup.bat
REM                or use setup.ps1 (PowerShell native, recommended)
REM
REM Idempotent: safe to run multiple times.
REM ASCII-only to avoid encoding problems.

cd /d "%~dp0"

set VENV_DIR=.venv

echo === Checking Python ===
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.9+ and add to PATH.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version') do echo   OK ^(Python %%v^)
echo.

REM Step 1: create venv
echo === Creating virtualenv ===
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo   Creating %VENV_DIR% ...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: failed to create venv
        pause
        exit /b 1
    )
) else (
    echo   %VENV_DIR% already exists, skipping
)
echo.

REM Step 2: install deps
echo === Installing dependencies ===
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip >nul
"%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed
    pause
    exit /b 1
)
echo   Done
echo.

REM Step 3: copy .env.example -> .env
echo === Copying .env.example to .env ===
if not exist ".env" (
    copy /Y .env.example .env >nul
    echo   [TODO] Edit .env with real API keys
) else (
    echo   .env already exists, skipping
)
echo.

echo =============================================
echo   Setup complete!
echo =============================================
echo.
echo Next steps:
echo   1. Edit .env with your OPENAI_API_KEY:
echo        notepad .env
echo.
echo   2. (Optional) Activate venv:
echo        %VENV_DIR%\Scripts\activate
echo.
echo   3. Run CLI:
echo        "%VENV_DIR%\Scripts\python.exe" -m companion_ever.cli setup --user alice --persona shushu --template hot_girlfriend
echo        "%VENV_DIR%\Scripts\python.exe" -m companion_ever.cli chat --user alice --persona shushu
echo.
echo (You can use %VENV_DIR%\Scripts\python.exe directly without activation)
echo.
pause
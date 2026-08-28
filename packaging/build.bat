@echo off
REM Build MOVoice.exe with PyInstaller (Windows, onedir).
REM Run this from the repository root:  packaging\build.bat
REM
REM NOTE: keep this file ASCII-only. cmd.exe reads .bat with the system
REM codepage (cp932 on Japanese Windows), so non-ASCII comments get mangled
REM and may be executed as commands.

setlocal

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Create it first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt pyinstaller
    exit /b 1
)

echo === Building MOVoice.exe ===
".venv\Scripts\pyinstaller.exe" packaging\MOVoice.spec --noconfirm --distpath dist --workpath build
if errorlevel 1 (
    echo [ERROR] Build failed.
    exit /b 1
)

echo.
echo === Done ===
echo Output: dist\MOVoice\MOVoice.exe
endlocal

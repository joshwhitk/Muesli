@echo off
setlocal
set "TEMP=%~dp0.tmp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%"

echo === Muesli Setup ===
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org ^(check "Add to PATH"^)
    pause
    exit /b 1
)

:: Create venv
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
)

echo Installing Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
where nvidia-smi >nul 2>&1
if not errorlevel 1 (
    echo NVIDIA GPU detected. Installing CUDA runtime packages for faster-whisper...
    ".venv\Scripts\python.exe" -m pip install nvidia-cublas-cu12 nvidia-cuda-runtime-cu12
    if errorlevel 1 (
        echo WARNING: CUDA runtime packages did not install. Whisper will fall back to CPU.
    )
)
echo Installing optional recording dependencies...
".venv\Scripts\python.exe" -m pip install sounddevice
".venv\Scripts\python.exe" -m pip install pyaudio
if errorlevel 1 (
    echo WARNING: PyAudio did not install. Muesli will fall back to sounddevice for recording if available.
)

echo.
echo === ffmpeg check ===
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo WARNING: ffmpeg not found. Recordings will be saved as WAV ^(larger files^).
    echo To enable MP3: winget install ffmpeg   OR   choco install ffmpeg
) else (
    echo ffmpeg OK
)

echo.
echo === Creating or refreshing Windows shortcuts ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh_windows_shortcuts.ps1"
if errorlevel 1 (
    echo WARNING: Windows shortcuts could not be refreshed automatically.
)

echo.
echo Setup complete. Run Muesli.lnk on your desktop, or: .venv\Scripts\python.exe muesli_gui.py
pause

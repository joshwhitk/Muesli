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
echo === Creating desktop shortcut ===
powershell -NoProfile -Command ^
  "$root = [System.IO.Path]::GetFullPath('%~dp0'); ^
   $ws = New-Object -ComObject WScript.Shell; ^
   $s  = $ws.CreateShortcut([System.IO.Path]::Combine($ws.SpecialFolders('Desktop'), 'Muesli.lnk')); ^
   $s.TargetPath   = [System.IO.Path]::Combine($root, '.venv\Scripts\pythonw.exe'); ^
   $s.Arguments    = '\"' + [System.IO.Path]::Combine($root, 'muesli_gui.py') + '\"'; ^
   $s.WorkingDirectory = $root; ^
   $icon = [System.IO.Path]::Combine($root, 'assets\muesli-icon.ico'); ^
   if ([System.IO.File]::Exists($icon)) { $s.IconLocation = $icon } ^
   else { $s.IconLocation = 'shell32.dll,168' } ^
   $s.Description  = 'Muesli'; ^
   $s.Save()"

powershell -NoProfile -Command ^
  "$root = [System.IO.Path]::GetFullPath('%~dp0'); ^
   $programs = [Environment]::GetFolderPath('Programs'); ^
   $ws = New-Object -ComObject WScript.Shell; ^
   $s = $ws.CreateShortcut([System.IO.Path]::Combine($programs, 'Muesli Record.lnk')); ^
   $s.TargetPath = [System.IO.Path]::Combine($root, '.venv\Scripts\pythonw.exe'); ^
   $s.Arguments = '\"' + [System.IO.Path]::Combine($root, 'muesli_gui.py') + '\" --record'; ^
   $s.WorkingDirectory = $root; ^
   $icon = [System.IO.Path]::Combine($root, 'assets\muesli-icon.ico'); ^
   if ([System.IO.File]::Exists($icon)) { $s.IconLocation = $icon; } ^
   $s.Description = 'Muesli Record'; ^
   $s.Hotkey = ''; ^
   $s.Save()"

powershell -NoProfile -Command ^
  "$root = [System.IO.Path]::GetFullPath('%~dp0'); ^
   $startup = [Environment]::GetFolderPath('Startup'); ^
   $ws = New-Object -ComObject WScript.Shell; ^
   $s = $ws.CreateShortcut([System.IO.Path]::Combine($startup, 'Muesli Hotkey.lnk')); ^
   $s.TargetPath = [System.IO.Path]::Combine($root, '.venv\Scripts\pythonw.exe'); ^
   $s.Arguments = '\"' + [System.IO.Path]::Combine($root, 'muesli_hotkey.py') + '\"'; ^
   $s.WorkingDirectory = $root; ^
   $icon = [System.IO.Path]::Combine($root, 'assets\muesli-icon.ico'); ^
   if ([System.IO.File]::Exists($icon)) { $s.IconLocation = $icon; } ^
   $s.Description = 'Muesli Hotkey (Ctrl+Shift+`)'; ^
   $s.Save()"

echo.
echo Setup complete. Run Muesli.lnk on your desktop, or: .venv\Scripts\python.exe muesli_gui.py
pause

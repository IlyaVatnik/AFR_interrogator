@echo off
setlocal
cd /d "%~dp0"

set "PYTHON="
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

if not defined PYTHON (
    where py >nul 2>nul
    if not errorlevel 1 set "PYTHON=py -3.11"
)

if not defined PYTHON set "PYTHON=python"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto error
)

set "PYTHON=.venv\Scripts\python.exe"

echo ==================================================
echo Building AFR interrogator 2.0.0
echo Python: %PYTHON%
echo ==================================================

%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 goto error

%PYTHON% -m PyInstaller --noconfirm --clean AFR_interrogator.spec
if errorlevel 1 goto error

if exist "dist\AFR_interrogator" (
    copy /Y "Params.txt" "dist\AFR_interrogator\Params.txt" >nul
    if not exist "dist\AFR_interrogator\data" mkdir "dist\AFR_interrogator\data"
)

echo ==================================================
echo Build finished: dist\AFR_interrogator
echo ==================================================
goto end

:error
echo ==================================================
echo Build failed
echo ==================================================

:end
if /I not "%~1"=="--no-pause" pause
endlocal

@echo off
setlocal
cd /d "%~dp0"

set "PYTHON="
if exist "C:\ProgramData\miniconda3\python.exe" set "PYTHON=C:\ProgramData\miniconda3\python.exe"
if not defined PYTHON if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

if not defined PYTHON (
    where py >nul 2>nul
    if not errorlevel 1 set "PYTHON=py -3.11"
)

if not defined PYTHON set "PYTHON=python"

echo ==================================================
echo Building AFR interrogator 2.0.0
echo Python: %PYTHON%
echo ==================================================

%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 goto error

%PYTHON% -m PyInstaller --noconfirm --clean --onedir --console --name AFR_interrogator --hidden-import pyqtgraph --exclude-module PyQt6 --exclude-module PySide6 --exclude-module PySide2 --exclude-module sklearn --exclude-module jupyter --exclude-module IPython --exclude-module notebook --exclude-module pandas --add-data "Params.txt;." GUI_interrogator.py
if errorlevel 1 goto error

if exist "dist\AFR_interrogator" (
    copy /Y "Params.txt" "dist\AFR_interrogator\Params.txt" >nul
    if not exist "dist\AFR_interrogator\data" mkdir "dist\AFR_interrogator\data"
)

if exist "C:\ProgramData\miniconda3\Library\bin" (
    for %%D in (
        freetype.dll libjpeg.dll libwebp.dll libwebpdemux.dll libwebpmux.dll
        libsharpyuv.dll lcms2.dll tiff.dll openjp2.dll zlib.dll libpng16.dll
        deflate.dll Lerc.dll zstd.dll liblzma.dll
    ) do (
        if exist "C:\ProgramData\miniconda3\Library\bin\%%D" (
            copy /Y "C:\ProgramData\miniconda3\Library\bin\%%D" "dist\AFR_interrogator\_internal\%%D" >nul
        )
    )
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

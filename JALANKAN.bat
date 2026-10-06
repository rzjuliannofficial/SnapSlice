@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ================================================
echo SnapSlice - Precision Image Splitter
echo ================================================
echo.

echo [1/5] Mencari Python 3.9+...
set "PY="
set "PY_ARGS="
set "PYVER="
set "PY_MAJOR="
set "PY_MINOR="

rem ------------------------------------------------
rem Coba Python Launcher terlebih dahulu.
rem Jangan hanya percaya --version; jalankan kode Python nyata.
rem ------------------------------------------------
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "TEST_MAJOR="
    set "TEST_MINOR="
    for /f "tokens=1,2" %%A in ('py -3 -c "import sys; print(sys.version_info[0],sys.version_info[1])" 2^>nul') do (
        set "TEST_MAJOR=%%A"
        set "TEST_MINOR=%%B"
    )
    if defined TEST_MAJOR (
        set /a TEST_CODE=TEST_MAJOR*100+TEST_MINOR
        if !TEST_CODE! GEQ 309 (
            set "PY=py"
            set "PY_ARGS=-3"
            set "PY_MAJOR=!TEST_MAJOR!"
            set "PY_MINOR=!TEST_MINOR!"
        )
    )
)

rem ------------------------------------------------
rem Fallback python.exe.
rem Tolak WindowsApps Microsoft Store alias.
rem ------------------------------------------------
if not defined PY (
    set "REAL_PYTHON="
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | find /I "\WindowsApps\" >nul
        if errorlevel 1 if not defined REAL_PYTHON set "REAL_PYTHON=%%P"
    )

    if defined REAL_PYTHON (
        set "TEST_MAJOR="
        set "TEST_MINOR="
        for /f "tokens=1,2" %%A in ('python -c "import sys; print(sys.version_info[0],sys.version_info[1])" 2^>nul') do (
            set "TEST_MAJOR=%%A"
            set "TEST_MINOR=%%B"
        )
        if defined TEST_MAJOR (
            set /a TEST_CODE=TEST_MAJOR*100+TEST_MINOR
            if !TEST_CODE! GEQ 309 (
                set "PY=python"
                set "PY_ARGS="
                set "PY_MAJOR=!TEST_MAJOR!"
                set "PY_MINOR=!TEST_MINOR!"
            )
        )
    )
)

if not defined PY (
    echo.
    echo Python 3.9+ tidak ditemukan atau tidak valid.
    echo.
    echo Silakan instal Python dari:
    echo https://www.python.org/downloads/
    echo.
    echo Saat instalasi Windows, pastikan:
    echo   - Add python.exe to PATH dicentang
    echo   - Tcl/Tk / IDLE ikut terpasang
    echo.
    echo Setelah instalasi selesai, tutup jendela ini lalu jalankan JALANKAN.bat lagi.
    echo.
    pause
    exit /b 1
)

set "PY_CMD=%PY% %PY_ARGS%"
set "PYVER=!PY_MAJOR!.!PY_MINOR!"
echo Python ditemukan: !PYVER!
echo Perintah: !PY_CMD!
echo.

echo [2/5] Mengecek tkinter...
%PY% %PY_ARGS% -c "import tkinter; print('Tkinter OK')"
if errorlevel 1 (
    echo.
    echo Tkinter TIDAK tersedia pada Python yang dipilih.
    echo Instal ulang Python dari https://www.python.org/downloads/
    echo dan pastikan Tcl/Tk ikut terpasang.
    echo.
    pause
    exit /b 1
)
echo.

echo [3/5] Mengecek Pillow dan NumPy...
%PY% %PY_ARGS% -c "import PIL, numpy; print('Pillow', PIL.__version__); print('NumPy', numpy.__version__)"
if errorlevel 1 (
    echo.
    echo Pillow atau NumPy belum terpasang. Menjalankan instalasi dependency...
    echo.
    %PY% %PY_ARGS% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo GAGAL memasang dependency.
        echo Coba jalankan manual:
        echo %PY_CMD% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo.
    echo Dependency berhasil dipasang. Mengecek ulang...
    %PY% %PY_ARGS% -c "import PIL, numpy; print('Pillow', PIL.__version__); print('NumPy', numpy.__version__)"
    if errorlevel 1 (
        echo.
        echo Dependency masih tidak bisa diimpor setelah instalasi.
        echo Periksa pesan pip di atas.
        echo.
        pause
        exit /b 1
    )
)
echo.

echo [4/5] Menjalankan self-check import aplikasi...
%PY% %PY_ARGS% -c "import runpy; print('Import/runtime dasar OK')" >nul 2>&1
if errorlevel 1 (
    echo.
    echo Python berhasil ditemukan, tetapi runtime Python bermasalah.
    echo Jalankan perintah berikut dari CMD untuk diagnosis:
    echo %PY_CMD% -c "import PIL, numpy, tkinter; print('OK')"
    echo.
    pause
    exit /b 1
)

echo [5/5] Membuka aplikasi...
echo Jangan tutup terminal ini selama aplikasi masih berjalan.
echo.
if exist "SnapSlice.py" (
    %PY% %PY_ARGS% "SnapSlice.py"
) else (
    %PY% %PY_ARGS% "pecah gambar.py"
)
set "APP_EXIT=%ERRORLEVEL%"

echo.
if not "!APP_EXIT!"=="0" (
    echo ================================================
    echo APLIKASI BERHENTI DENGAN ERROR
    echo Kode keluar: !APP_EXIT!
    echo Detail: log_error.txt di folder aplikasi
    echo ================================================
    echo.
    echo Terminal akan tetap terbuka supaya pesan error dapat dibaca.
    pause
    exit /b !APP_EXIT!
)

echo Aplikasi selesai dengan normal.
exit /b 0

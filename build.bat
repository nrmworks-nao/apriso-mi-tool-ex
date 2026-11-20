@echo off
echo ========================================
echo  AprisoMIToolEx Build Script
echo ========================================

rem Activate venv
call .venv\Scripts\activate.bat

rem Clean previous build
if exist dist (
    echo Cleaning dist...
    rmdir /s /q dist
)
if exist build (
    echo Cleaning build...
    rmdir /s /q build
)

echo.
echo Building...

rem Build with PyInstaller
pyinstaller --noconfirm AprisoMIToolEx.spec

if errorlevel 1 (
    echo.
    echo ========================================
    echo  BUILD FAILED
    echo ========================================
    pause
    exit /b 1
)

rem Copy README
if exist README.txt (
    copy README.txt dist\AprisoMIToolEx\ > nul
    echo README.txt copied.
)

echo.
echo ========================================
echo  BUILD OK: dist\AprisoMIToolEx\
echo ========================================
pause

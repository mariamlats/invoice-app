@echo off
echo ================================
echo  Invoice Manager - Build Script
echo ================================
echo.

echo Step 1: Installing dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
python -m pip install pywin32 pyinstaller
if errorlevel 1 exit /b 1

echo.
echo Step 2: Building .exe...
pyinstaller invoice.spec --clean

echo.
echo Step 3: Configuration...
echo Configure .env separately on each trusted installation.
echo Secrets are never copied into the distributable folder.

echo.
echo ================================
if exist "dist\InvoiceManager.exe" (
    echo  BUILD SUCCESSFUL!
    echo  Distribute only the executable. Configure credentials separately.
    echo  Users just double-click InvoiceManager.exe
) else (
    echo  BUILD FAILED - check errors above
)
echo ================================
pause


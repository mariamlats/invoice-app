@echo off
echo ================================
echo  Invoice Manager - Build Script
echo ================================
echo.

echo Step 1: Installing dependencies...
pip install flask flask-sqlalchemy reportlab pywin32 psycopg2-binary pyinstaller python-dotenv

echo.
echo Step 2: Building .exe...
pyinstaller invoice.spec --clean

echo.
echo ================================
if exist "dist\InvoiceManager.exe" (
    echo  BUILD SUCCESSFUL!
    echo  Your .exe is at: dist\InvoiceManager.exe
    echo  Copy that file to any Windows laptop and double-click to run.
) else (
    echo  BUILD FAILED - check errors above
)
echo ================================
pause

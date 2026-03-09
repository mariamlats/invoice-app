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
echo Step 3: Copying .env to dist...
if exist ".env" (
    copy ".env" "dist\.env"
    echo .env copied to dist/
) else (
    echo WARNING: .env not found - remember to put it in dist/ manually!
)

echo.
echo ================================
if exist "dist\InvoiceManager.exe" (
    echo  BUILD SUCCESSFUL!
    echo  Distribute the entire dist\ folder.
    echo  Users just double-click InvoiceManager.exe
) else (
    echo  BUILD FAILED - check errors above
)
echo ================================
pause

# Invoice Manager — Build Guide

## To build the .exe (do this once on your main laptop)

**1. Open Command Prompt in the invoice-app folder:**
```
cd Desktop\invoice-app
```

**2. Double-click `build.bat`** or run in cmd:
```
build.bat
```

This will:
- Install all dependencies automatically
- Build `dist\InvoiceManager.exe`

Wait 3-5 minutes for it to finish.

**3. Done!** Your .exe is at:
```
Desktop\invoice-app\dist\InvoiceManager.exe
```

---

## To distribute to other laptops

Just copy `InvoiceManager.exe` to any Windows laptop and double-click.
- No Python needed
- No setup needed
- Browser opens automatically
- All devices share the same database (Railway PostgreSQL)

---

## Database
Hosted on Railway (PostgreSQL). Connection is built into the .exe.
All companies and invoices are shared across all devices automatically.

"""
Imports 50 companies from the Excel file.
import_companies.py
Run this once to import all companies from the Excel into the database.
Usage: python import_companies.py
"""
import zipfile
import xml.etree.ElementTree as ET
import re
import os
import sys

# ── Setup Flask app context ──────────────────────────────────────────────────
os.environ.setdefault('DATABASE_URL', 'postgresql://postgres:DemixInvoice1@db.tocstcvgltxhzuuizwrx.supabase.co:5432/postgres')

from app import app, db, Company

def clean_vat(raw):
    """Convert scientific notation VAT codes like 4.06313911E8 to integer string."""
    if not raw:
        return ''
    try:
        # Handle scientific notation
        val = float(raw)
        return str(int(val))
    except Exception:
        return str(raw).strip()

def clean_email(raw):
    """Return all valid emails joined by comma."""
    if not raw:
        return ''
    parts = re.split(r'[\s;,]+', raw.strip())
    emails = [p.strip() for p in parts if p.strip() and '@' in p and '.' in p]
    return ', '.join(emails)

def read_excel(path):
    with zipfile.ZipFile(path, 'r') as z:
        ss_xml = z.read('xl/sharedStrings.xml')
        ss_root = ET.fromstring(ss_xml)
        ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        strings = []
        for si in ss_root.findall('{%s}si' % ns):
            text = ''.join(t.text or '' for t in si.iter('{%s}t' % ns))
            strings.append(text)

        sheet_xml = z.read('xl/worksheets/sheet1.xml')
        sheet_root = ET.fromstring(sheet_xml)

        rows = {}
        for row in sheet_root.findall('.//{%s}row' % ns):
            rnum = int(row.get('r'))
            rows[rnum] = {}
            for cell in row.findall('{%s}c' % ns):
                ref = cell.get('r')
                col = ''.join(c for c in ref if c.isalpha())
                t = cell.get('t')
                v_el = cell.find('{%s}v' % ns)
                val = v_el.text if v_el is not None else None
                if t == 's' and val is not None:
                    val = strings[int(val)]
                rows[rnum][col] = val
    return rows

def main():
    excel_path = 'companies.xlsx'
    if not os.path.exists(excel_path):
        print('ERROR: Excel file not found: %s' % excel_path)
        print('Place the Excel file in the same folder as this script.')
        sys.exit(1)

    print('Reading Excel...')
    rows = read_excel(excel_path)

    imported = 0
    skipped  = 0
    errors   = 0

    with app.app_context():
        db.create_all()

        for rnum in sorted(rows.keys()):
            if rnum == 1:
                continue  # skip header row

            row  = rows[rnum]
            name = (row.get('B') or '').strip()
            vat  = clean_vat(row.get('C'))
            email = clean_email(row.get('F') or '')

            # Skip empty rows
            if not name:
                continue

            # Skip if company already exists by VAT
            if vat and Company.query.filter_by(vat=vat).first():
                print('  SKIP (already exists): %s' % name)
                skipped += 1
                continue

            # Skip if no email at all
            if not email:
                print('  SKIP (no email): %s' % name)
                skipped += 1
                continue

            try:
                co = Company(
                    name    = name,
                    vat     = vat or 'N/A',
                    address = 'თბილისი',
                    email   = email,
                )
                db.session.add(co)
                db.session.commit()
                print('  OK: %s (%s)' % (name, email))
                imported += 1
            except Exception as e:
                db.session.rollback()
                print('  ERROR: %s — %s' % (name, e))
                errors += 1

    print()
    print('=' * 50)
    print('Done!')
    print('  Imported: %d' % imported)
    print('  Skipped:  %d' % skipped)
    print('  Errors:   %d' % errors)
    print('=' * 50)

if __name__ == '__main__':
    main()

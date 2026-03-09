import sys
import os
import threading
import time
import webbrowser

# ── Fix paths when running as a PyInstaller .exe ──────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    os.chdir(os.path.dirname(sys.executable))
    os.environ['FLASK_TEMPLATE_FOLDER'] = os.path.join(BASE_DIR, 'templates')
    os.environ['FLASK_STATIC_FOLDER']   = os.path.join(BASE_DIR, 'static')
    os.environ['FONT_DIR']              = os.path.join(BASE_DIR, 'fonts')
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load .env from same folder as the .exe (or script) ────────────────────────
env_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else BASE_DIR, '.env')
if os.path.exists(env_path):
    from dotenv import load_dotenv
    load_dotenv(env_path)

PORT = 5000
URL  = 'http://localhost:%d' % PORT

def open_browser():
    time.sleep(2.5)
    webbrowser.open(URL)

def run_flask():
    from app import app, db
    with app.app_context():
        db.create_all()
        import sqlalchemy
        with db.engine.connect() as conn:
            # All columns that may be missing on older DBs
            migrations = [
                ('invoice',  'send_error',       'VARCHAR(255)'),
                ('invoice',  'custom_date',       'VARCHAR(20)'),
                ('tenant',   'footer_name',       'VARCHAR(255)'),
                ('tenant',   'footer_vat',        'VARCHAR(100)'),
                ('tenant',   'footer_address',    'VARCHAR(500)'),
                ('tenant',   'footer_phone',      'VARCHAR(100)'),
                ('tenant',   'footer_email',      'VARCHAR(255)'),
                ('tenant',   'footer_bank',       'VARCHAR(255)'),
                ('tenant',   'footer_bank_code',  'VARCHAR(100)'),
                ('tenant',   'footer_iban',       'VARCHAR(100)'),
                ('tenant',   'footer_director',   'VARCHAR(255)'),
                ('tenant',   'signature_path',    'VARCHAR(500)'),
                ('tenant',   'invoice_prefix',    'INTEGER DEFAULT 30000'),
                ('tenant',   'smtp_email',        'VARCHAR(255)'),
                ('tenant',   'smtp_password',     'VARCHAR(255)'),
                ('tenant',   'smtp_host',         'VARCHAR(255)'),
                ('tenant',   'smtp_port',         'INTEGER DEFAULT 465'),
                ('tenant',   'email_from',        'VARCHAR(255)'),
                ('tenant',   'email_subject',     'VARCHAR(500)'),
                ('tenant',   'email_body',        'TEXT'),
            ]
            for table, col, typ in migrations:
                try:
                    conn.execute(sqlalchemy.text(
                        'ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s' % (table, col, typ)
                    ))
                    conn.commit()
                except Exception:
                    pass
    app.run(host='127.0.0.1', port=PORT, debug=False, use_reloader=False)

if __name__ == '__main__':
    print('Starting Invoice Manager...')
    print('Opening browser at', URL)
    t = threading.Thread(target=open_browser, daemon=True)
    t.start()
    run_flask()

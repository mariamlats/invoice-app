import sys
import os
import threading
import time
import webbrowser

# ── Fix paths when running as a PyInstaller .exe ──────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    # Set working directory to the folder containing the .exe
    os.chdir(os.path.dirname(sys.executable))
    # Point Flask to bundled templates/static
    os.environ['FLASK_TEMPLATE_FOLDER'] = os.path.join(BASE_DIR, 'templates')
    os.environ['FLASK_STATIC_FOLDER']   = os.path.join(BASE_DIR, 'static')
    os.environ['FONT_DIR']              = os.path.join(BASE_DIR, 'fonts')
    os.environ['SIG_PATH']              = os.path.join(BASE_DIR, 'static', 'signature.png')
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

os.environ.setdefault('DATABASE_URL', 'postgresql://postgres.tocstcvgltxhzuuizwrx:DemixInvoice1@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres')

PORT = 5000
URL  = 'http://localhost:%d' % PORT

def open_browser():
    time.sleep(2.5)  # wait for Flask to start
    webbrowser.open(URL)

def run_flask():
    from app import app, db
    with app.app_context():
        db.create_all()
        # Migrate new columns if needed
        import sqlalchemy
        with db.engine.connect() as conn:
            for col, typ in [('send_error', 'VARCHAR(255)'), ('custom_date', 'VARCHAR(20)')]:
                try:
                    conn.execute(sqlalchemy.text('ALTER TABLE invoice ADD COLUMN %s %s' % (col, typ)))
                    conn.commit()
                except Exception:
                    pass
    app.run(host='127.0.0.1', port=PORT, debug=False, use_reloader=False)

if __name__ == '__main__':
    print('Starting Invoice Manager...')
    print('Opening browser at', URL)

    # Start browser opener in background
    t = threading.Thread(target=open_browser, daemon=True)
    t.start()

    # Run Flask (blocking)
    run_flask()

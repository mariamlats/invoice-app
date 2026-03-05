from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Image
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os, io, json, base64, sys

# Load .env file — works both normally and when frozen as .exe
try:
    from dotenv import load_dotenv
    if getattr(sys, 'frozen', False):
        # .env sits next to the .exe
        _env_path = os.path.join(os.path.dirname(sys.executable), '.env')
    else:
        _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    load_dotenv(_env_path)
except ImportError:
    pass  # dotenv not installed, fall back to environment variables

if getattr(sys, 'frozen', False):
    app = Flask(
        __name__,
        template_folder=os.environ.get('FLASK_TEMPLATE_FOLDER', 'templates'),
        static_folder=os.environ.get('FLASK_STATIC_FOLDER', 'static'),
    )
else:
    app = Flask(__name__)

_db_url = os.environ.get('DATABASE_URL')
if not _db_url:
    raise RuntimeError('DATABASE_URL not set. Please create a .env file — see .env.example')
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ── Paths ─────────────────────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    DATA_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BASE_DIR

FONT_DIR = os.environ.get('FONT_DIR', os.path.join(BASE_DIR, 'fonts'))

import platform
if platform.system() == 'Windows':
    WIN_FONTS = 'C:/Windows/Fonts/'
    pdfmetrics.registerFont(TTFont('DejaVu',     WIN_FONTS + 'segoeui.ttf'))
    pdfmetrics.registerFont(TTFont('DejaVuBold', WIN_FONTS + 'segoeuib.ttf'))
else:
    noto_path = os.path.join(FONT_DIR, 'NotoSansGeorgian.ttf')
    main_font = noto_path if os.path.exists(noto_path) else os.path.join(FONT_DIR, 'DejaVuSans.ttf')
    pdfmetrics.registerFont(TTFont('DejaVu',     main_font))
    pdfmetrics.registerFont(TTFont('DejaVuBold', os.path.join(FONT_DIR, 'DejaVuSans-Bold.ttf')))

SIG_PATH  = os.environ.get('SIG_PATH', os.path.join(BASE_DIR, 'static', 'signature.png'))

FIRST_NUM = 30281

# ── Models ────────────────────────────────────────────────────────────────────

class Company(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    legal_form = db.Column(db.String(20), nullable=False, default='შპს')
    name       = db.Column(db.String(255), nullable=False)
    vat        = db.Column(db.String(100), nullable=False)
    address    = db.Column(db.String(500), nullable=False)
    email      = db.Column(db.String(255), nullable=False)
    status     = db.Column(db.String(20), nullable=False, default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'legal_form': self.legal_form, 'name': self.name,
                'vat': self.vat, 'address': self.address, 'email': self.email,
                'status': self.status}

class Product(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(255), nullable=False)
    unit       = db.Column(db.String(50), nullable=False)
    price      = db.Column(db.String(50), nullable=False)
    vat        = db.Column(db.String(5), nullable=False, default='no')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'unit': self.unit, 'price': self.price, 'vat': self.vat}

class Invoice(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    number       = db.Column(db.Integer, nullable=False, unique=True)
    company_id   = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    amount       = db.Column(db.String(50), nullable=False)
    items        = db.Column(db.Text, nullable=True)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    sent         = db.Column(db.Boolean, default=False)
    send_error   = db.Column(db.String(255), nullable=True)
    custom_date  = db.Column(db.String(20), nullable=True)
    company      = db.relationship('Company', backref='invoices')

    def to_dict(self):
        return {
            'id':     self.id,
            'number': self.number,
            'company': self.company.to_dict() if self.company else None,
            'amount': self.amount,
            'items':  json.loads(self.items) if self.items else [],
            'sent':   self.sent,
            'error':  self.send_error,
            'date':   self.generated_at.strftime('%d/%m/%Y'),
        }

# ── PDF Builder ───────────────────────────────────────────────────────────────

def build_pdf(invoice, show_details=True):
    buf   = io.BytesIO()
    W, H  = A4
    mg    = 1.5 * cm

    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=mg, rightMargin=mg, topMargin=mg, bottomMargin=mg)

    c        = invoice.company
    date_str = invoice.custom_date if invoice.custom_date else invoice.generated_at.strftime('%d/%m/%Y')
    uw       = W - 2 * mg

    BLUE  = colors.HexColor('#2C5F8A')
    LIGHT = colors.HexColor('#EBF3FA')
    GRAY  = colors.HexColor('#CCCCCC')
    LGRID = colors.HexColor('#A8C8E8')

    def p(text, size=9, bold=False, align='LEFT', color=colors.black, leading=None):
        return Paragraph(text, ParagraphStyle('x',
            fontName='DejaVuBold' if bold else 'DejaVu',
            fontSize=size, textColor=color,
            alignment={'LEFT':0,'CENTER':1,'RIGHT':2}[align],
            leading=leading or size*1.4))

    # Title bar
    t1 = Table([[p('შპს დემიქსი &nbsp;&nbsp; ინვოისი N %d' % invoice.number,
                   size=15, bold=True, align='CENTER', color=colors.white)]],
               colWidths=[uw])
    t1.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),BLUE),
        ('TOPPADDING',(0,0),(-1,-1),9),('BOTTOMPADDING',(0,0),(-1,-1),9),
        ('LEFTPADDING',(0,0),(-1,-1),10),
    ]))

    # შემკვეთი + date
    t2 = Table([[p('შემკვეთი:', bold=True, size=11),
                 p(date_str, bold=True, align='RIGHT', size=11)]],
               colWidths=[uw*0.62, uw*0.38])
    t2.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),LIGHT),
        ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),
        ('LEFTPADDING',(0,0),(0,-1),10),('RIGHTPADDING',(1,0),(1,-1),10),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
    ]))

    # Client block
    full_name = '%s %s' % (c.legal_form or 'შპს', c.name)
    ct = '<b>%s</b><br/>ს/კ: %s<br/>იურიდიული მისამართი: %s<br/>ელ. მეილი: %s' % (
         full_name, c.vat, c.address, c.email)
    t3 = Table([[p(ct, leading=17, size=11)]], colWidths=[uw])
    t3.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),colors.white),
        ('BOX',(0,0),(-1,-1),0.5,GRAY),
        ('TOPPADDING',(0,0),(-1,-1),12),('BOTTOMPADDING',(0,0),(-1,-1),12),
        ('LEFTPADDING',(0,0),(-1,-1),10),
        ('VALIGN',(0,0),(-1,-1),'TOP'),('ROWHEIGHT',(0,0),(-1,-1),75),
    ]))

    # Products table
    cw = [uw*0.05, uw*0.31, uw*0.13, uw*0.15, uw*0.18, uw*0.18]
    items = []
    if invoice.items:
        try: items = json.loads(invoice.items)
        except: pass

    if show_details:
        hdr = [
            p('#', bold=True, align='CENTER', color=colors.white, size=10),
            p('პროდუქტის დასახელება', bold=True, align='CENTER', color=colors.white, size=10),
            p('ზომის\nერთეული', bold=True, align='CENTER', color=colors.white, size=10),
            p('რაოდენობა', bold=True, align='CENTER', color=colors.white, size=10),
            p('ერთეულის ფასი', bold=True, align='CENTER', color=colors.white, size=10),
            p('ღირებულება', bold=True, align='CENTER', color=colors.white, size=10),
        ]
        data_rows = [[
            p(str(i), align='CENTER', size=10),
            p(item.get('name',''), size=10, leading=14),
            p(item.get('unit',''), align='CENTER', size=10),
            p(str(item.get('qty','')), align='CENTER', size=10),
            p(str(item.get('price','')), align='CENTER', size=10),
            p(str(item.get('total','')), align='CENTER', size=10),
        ] for i, item in enumerate(items, 1)]
        total_row = [p(''),p(''),p(''),p(''), p('სულ:', bold=True, align='RIGHT', size=11), p(str(invoice.amount) + ' ₾', bold=True, align='CENTER', size=11)]
    else:
        cw = [uw*0.06, uw*0.74, uw*0.20]
        hdr = [
            p('#', bold=True, align='CENTER', color=colors.white, size=10),
            p('პროდუქტის დასახელება', bold=True, align='CENTER', color=colors.white, size=10),
            p('ღირებულება', bold=True, align='CENTER', color=colors.white, size=10),
        ]
        data_rows = [[
            p(str(i), align='CENTER', size=10),
            p(item.get('name',''), size=10, leading=14),
            p(str(item.get('total','')), align='CENTER', size=10),
        ] for i, item in enumerate(items, 1)]
        total_row = [p(''), p('სულ:', bold=True, align='RIGHT', size=11), p(str(invoice.amount) + ' ₾', bold=True, align='CENTER', size=11)]

    rows = [hdr] + data_rows + [total_row]

    nr = len(rows)
    st = [
        ('BACKGROUND',(0,0),(-1,0),BLUE),
        ('GRID',(0,0),(-1,-1),0.5,LGRID),
        ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),
        ('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('BACKGROUND',(0,nr-1),(-1,nr-1),LIGHT),
        ('LINEABOVE',(0,nr-1),(-1,nr-1),1.0,BLUE),
    ]
    for ri in range(1, nr-1):
        st.append(('BACKGROUND',(0,ri),(-1,ri), LIGHT if ri%2==1 else colors.white))

    t_items = Table(rows, colWidths=cw)
    t_items.setStyle(TableStyle(st))

    # შემსრულებელი label
    t6 = Table([[p('შემსრულებელი:', bold=True, size=11)]], colWidths=[uw])
    t6.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),LIGHT),
        ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),
        ('LEFTPADDING',(0,0),(-1,-1),10),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
    ]))

    # Executor + signature
    executor = (
        '<b>შპს დემიქსი</b> &nbsp; ს/კ 405328998<br/>'
        'ქინძმარაულის ქუჩა #17<br/>ტელ: 599 787 453<br/>'
        'მეილი: info@demix.ge<br/>ბანკი: JSC &quot;Bank of Georgia&quot;<br/>'
        'Bank code: BAGAGE22<br/>A/A: GE30BG0000000161105533'
    )
    sig_img = Image(SIG_PATH, width=6*cm, height=6*cm) if os.path.exists(SIG_PATH) else p('')
    rc = Table([
        [p('დირექტორი<br/>გიორგი გოგოლაძე', size=10, leading=15, align='CENTER')],
        [sig_img],
    ], colWidths=[uw*0.38])
    rc.setStyle(TableStyle([
        ('ALIGN',(0,0),(-1,-1),'CENTER'),('VALIGN',(0,0),(-1,-1),'TOP'),
        ('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),0),
        ('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),
    ]))
    t7 = Table([[p(executor, size=10, leading=15), rc]], colWidths=[uw*0.62, uw*0.38])
    t7.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),colors.white),
        ('BOX',(0,0),(-1,-1),0.5,GRAY),('LINEAFTER',(0,0),(0,-1),0.5,GRAY),
        ('TOPPADDING',(0,0),(-1,-1),12),('BOTTOMPADDING',(0,0),(-1,-1),12),
        ('LEFTPADDING',(0,0),(0,0),10),('VALIGN',(0,0),(-1,-1),'TOP'),
    ]))

    doc.build([t1, t2, t3, t_items, t6, t7])
    buf.seek(0)
    return buf.read()

# ── Helpers ───────────────────────────────────────────────────────────────────

def next_invoice_number():
    last = db.session.query(db.func.max(Invoice.number)).scalar()
    return (last + 1) if last else FIRST_NUM

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/companies', methods=['GET'])
def get_companies():
    return jsonify([c.to_dict() for c in Company.query.order_by(Company.name).all()])

@app.route('/api/companies', methods=['POST'])
def create_company():
    d = request.get_json()
    co = Company(name=d['name'].strip(), vat=d['vat'].strip(),
                 address=d['address'].strip(), email=d['email'].strip())
    db.session.add(co); db.session.commit()
    return jsonify(co.to_dict()), 201

@app.route('/api/companies/<int:cid>', methods=['PUT'])
def update_company(cid):
    co = Company.query.get_or_404(cid)
    d  = request.get_json()
    email = d['email'].strip()
    for e in email.split(','):
        if '@' not in e.strip():
            return jsonify({'error': 'მეილები უნდა იყოს გამოყოფილი მძიმით'}), 400
    co.legal_form = d.get('legal_form', 'შპს').strip()
    co.name       = d['name'].strip()
    co.vat        = d['vat'].strip()
    co.address    = d['address'].strip()
    co.email      = email
    co.status     = d.get('status', 'active')
    db.session.commit()
    return jsonify(co.to_dict())

@app.route('/api/companies/<int:cid>', methods=['DELETE'])
def delete_company(cid):
    co = Company.query.get_or_404(cid)
    # Remove invoices referencing this company first
    for inv in co.invoices:
        db.session.delete(inv)
    db.session.delete(co)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/products', methods=['GET'])
def get_products():
    return jsonify([p.to_dict() for p in Product.query.order_by(Product.name).all()])

@app.route('/api/products', methods=['POST'])
def create_product():
    d = request.get_json()
    pr = Product(name=d['name'].strip(), unit=d['unit'].strip(), price=d['price'].strip(), vat=d.get('vat','no'))
    db.session.add(pr); db.session.commit()
    return jsonify(pr.to_dict()), 201

@app.route('/api/products/<int:pid>', methods=['PUT'])
def update_product(pid):
    pr = Product.query.get_or_404(pid)
    d  = request.get_json()
    pr.name=d['name'].strip(); pr.unit=d['unit'].strip(); pr.price=d['price'].strip(); pr.vat=d.get('vat','no')
    db.session.commit()
    return jsonify(pr.to_dict())

@app.route('/api/products/<int:pid>', methods=['DELETE'])
def delete_product(pid):
    db.session.delete(Product.query.get_or_404(pid)); db.session.commit()
    return jsonify({'success': True})

@app.route('/api/invoice-number', methods=['GET'])
def get_invoice_number():
    return jsonify({'number': next_invoice_number()})

@app.route('/api/invoices/generate', methods=['POST'])
def generate_invoice():
    try:
        d           = request.get_json()
        company     = Company.query.get_or_404(d['company_id'])
        custom_date = d.get('date', '').strip()
        items       = d.get('items', [])

        total = 0.0
        for item in items:
            try: total += float(str(item.get('total','0')).replace(',','.'))
            except: pass
        amount_str = ('%.2f' % total) if total else '0.00'

        show_details = d.get('show_details', True)
        inv = Invoice(
            number      = next_invoice_number(),
            company_id  = company.id,
            amount      = amount_str,
            items       = json.dumps(items, ensure_ascii=False),
            custom_date = custom_date if custom_date else None,
        )
        db.session.add(inv); db.session.commit()
        pdf_b64 = base64.b64encode(build_pdf(inv, show_details=show_details)).decode()
        return jsonify({'invoice_id': inv.id, 'invoice_number': inv.number, 'pdf': pdf_b64})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/invoices/<int:inv_id>/send', methods=['POST'])
def send_invoice(inv_id):
    inv = Invoice.query.get_or_404(inv_id)
    if inv.sent:
        return jsonify({'error': 'Already sent'}), 400

    pdf_bytes = build_pdf(inv)
    tmp_path  = os.path.join(DATA_DIR, 'demix-invoice-%d.pdf' % inv.number)
    with open(tmp_path, 'wb') as f:
        f.write(pdf_bytes)

    try:
        import win32com.client as win32, pythoncom, time
        pythoncom.CoInitialize()
        outlook = win32.Dispatch('outlook.application')
        mail    = outlook.CreateItem(0)
        mapi    = outlook.GetNamespace('MAPI')
        sender_account = None
        for i in range(1, mapi.Accounts.Count + 1):
            acc = mapi.Accounts.Item(i)
            if acc.SmtpAddress.lower() == 'info@sawkobi.ge':
                sender_account = acc; break
        if not sender_account:
            inv.send_error = 'Account info@sawkobi.ge not found in Outlook'
            db.session.commit()
            return jsonify({'error': inv.send_error}), 400
        mail.To      = '; '.join([e.strip() for e in inv.company.email.split(',') if e.strip()])
        mail.Subject = 'Invoice N%d - შპს დემიქსი' % inv.number
        mail._oleobj_.Invoke(*(64209, 0, 8, 0, sender_account))
        our_text = '<p style="font-family:Calibri,sans-serif;font-size:11pt;">გთხოვთ იხილოთ თანდართული ინვოისი N%d.</p>' % inv.number
        mail.Display(False); time.sleep(1.5)
        mail.GetInspector
        mail.HTMLBody = our_text + (mail.HTMLBody or '')
        att = mail.Attachments.Add(os.path.abspath(tmp_path))
        att.DisplayName = 'demix-invoice-%d.pdf' % inv.number
        mail.Send()
        inv.sent = True; inv.send_error = None
        db.session.commit()
        return jsonify({'success': True})
    except ImportError:
        return jsonify({'error': 'pywin32 not installed'}), 500
    except Exception as e:
        inv.send_error = str(e); db.session.commit()
        return jsonify({'error': str(e)}), 500
    finally:
        try:
            import pythoncom; pythoncom.CoUninitialize()
        except: pass
        if os.path.exists(tmp_path): os.remove(tmp_path)

@app.route('/api/invoices', methods=['GET'])
def get_invoices():
    return jsonify([i.to_dict() for i in Invoice.query.order_by(Invoice.generated_at.desc()).all()])

@app.route('/api/invoices/<int:inv_id>', methods=['DELETE'])
def delete_invoice(inv_id):
    inv = Invoice.query.get_or_404(inv_id)
    if inv.sent:
        return jsonify({'error': 'Cannot delete a sent invoice'}), 400
    db.session.delete(inv); db.session.commit()
    return jsonify({'success': True})

def run_migrations():
    import sqlalchemy
    migrations = [
        ('invoice', 'send_error', 'VARCHAR(255)'),
        ('invoice', 'custom_date', 'VARCHAR(20)'),
        ('invoice', 'items',       'TEXT'),
        ('company', 'legal_form',  "VARCHAR(20) DEFAULT 'შპს'"),
        ('company', 'status',      "VARCHAR(20) DEFAULT 'active'"),
        ('product', 'vat',         "VARCHAR(5) DEFAULT 'no'"),
    ]
    for table, col, typ in migrations:
        # Use a fresh connection per statement so one failure doesn't poison the rest
        try:
            with db.engine.connect() as conn:
                conn.execute(sqlalchemy.text(
                    'ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s' % (table, col, typ)
                ))
                conn.commit()
        except Exception:
            pass

with app.app_context():
    db.create_all()
    run_migrations()

if __name__ == '__main__':
    app.run(debug=True)

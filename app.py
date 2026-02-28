from flask import Flask, render_template, request, jsonify, send_file
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

if getattr(sys, 'frozen', False):
    app = Flask(
        __name__,
        template_folder=os.environ.get('FLASK_TEMPLATE_FOLDER', 'templates'),
        static_folder=os.environ.get('FLASK_STATIC_FOLDER', 'static'),
    )
else:
    app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'postgresql://postgres:DemixInvoice1@db.tocstcvgltxhzuuizwrx.supabase.co:5432/postgres')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ── Paths — work both normally and when frozen as .exe ───────────────────────
import sys
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    DATA_DIR = os.path.dirname(sys.executable)  # writable dir next to .exe
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BASE_DIR

FONT_DIR = os.environ.get('FONT_DIR', os.path.join(BASE_DIR, 'fonts'))

# Use Segoe UI (built into Windows, supports Georgian + ₾)
# Fall back to DejaVu if not on Windows
import platform
if platform.system() == 'Windows':
    WIN_FONTS = 'C:/Windows/Fonts/'
    pdfmetrics.registerFont(TTFont('DejaVu',     WIN_FONTS + 'segoeui.ttf'))
    pdfmetrics.registerFont(TTFont('DejaVuBold', WIN_FONTS + 'segoeuib.ttf'))
else:
    pdfmetrics.registerFont(TTFont('DejaVu',     os.path.join(FONT_DIR, 'DejaVuSans.ttf')))
    pdfmetrics.registerFont(TTFont('DejaVuBold', os.path.join(FONT_DIR, 'DejaVuSans-Bold.ttf')))

SIG_PATH  = os.environ.get('SIG_PATH', os.path.join(BASE_DIR, 'static', 'signature.png'))
FIRST_NUM = 30281

# ── Models ────────────────────────────────────────────────────────────────────

class Company(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(255), nullable=False)
    vat        = db.Column(db.String(100), nullable=False)
    address    = db.Column(db.String(500), nullable=False)
    email      = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'vat': self.vat,
                'address': self.address, 'email': self.email}

class Invoice(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    number       = db.Column(db.Integer, nullable=False, unique=True)
    company_id   = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    amount       = db.Column(db.String(50), nullable=False)
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
            'sent':   self.sent,
            'error':  self.send_error,
            'date':   self.generated_at.strftime('%d/%m/%Y'),
        }

# ── PDF Builder ───────────────────────────────────────────────────────────────

def build_pdf(invoice):
    buf    = io.BytesIO()
    W, H   = A4
    margin = 1.5 * cm

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin,  bottomMargin=margin,
    )

    c         = invoice.company
    date_str  = invoice.custom_date if invoice.custom_date else invoice.generated_at.strftime('%d/%m/%Y')
    usable_w  = W - 2 * margin

    PURPLE = colors.HexColor('#2C5F8A')
    LIGHT  = colors.HexColor('#EBF3FA')
    GRAY   = colors.HexColor('#CCCCCC')

    def p(text, font='DejaVu', size=9, bold=False, align='LEFT', color=colors.black, leading=None):
        style = ParagraphStyle(
            'x',
            fontName='DejaVuBold' if bold else 'DejaVu',
            fontSize=size,
            textColor=color,
            alignment={'LEFT': 0, 'CENTER': 1, 'RIGHT': 2}[align],
            leading=leading or size * 1.4,
        )
        return Paragraph(text, style)

    # ── Row 1: Title bar ──────────────────────────────────────────────────────
    t1 = Table([[p('შპს დემიქსი &nbsp;&nbsp; ინვოისი N %d' % invoice.number,
                   size=15, bold=True, align='CENTER', color=colors.white)]],
               colWidths=[usable_w])
    t1.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), PURPLE),
        ('TOPPADDING',    (0,0), (-1,-1), 9),
        ('BOTTOMPADDING', (0,0), (-1,-1), 9),
        ('LEFTPADDING',   (0,0), (-1,-1), 10),
    ]))

    # ── Row 2: შემკვეთი + date ────────────────────────────────────────────────
    cw2 = [usable_w * 0.62, usable_w * 0.38]
    t2  = Table([[
        p('შემკვეთი:', bold=True, size=11),
        p(date_str, bold=True, align='RIGHT', size=11),
    ]], colWidths=cw2)
    t2.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), LIGHT),
        ('TOPPADDING',    (0,0), (-1,-1), 7),
        ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING',   (0,0), (0,-1),  10),
        ('RIGHTPADDING',  (1,0), (1,-1),  10),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
    ]))

    # ── Row 3: Client company block ───────────────────────────────────────────
    company_text = (
        '<b>%s</b><br/>'
        'ს/კ: %s<br/>'
        'იურიდიული მისამართი: %s<br/>'
        'ელ. მეილი: %s'
    ) % (c.name, c.vat, c.address, c.email)

    t3 = Table([[p(company_text, leading=17, size=11)]], colWidths=[usable_w])
    t3.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), colors.white),
        ('BOX',           (0,0), (-1,-1), 0.5, GRAY),
        ('TOPPADDING',    (0,0), (-1,-1), 12),
        ('BOTTOMPADDING', (0,0), (-1,-1), 12),
        ('LEFTPADDING',   (0,0), (-1,-1), 10),
        ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ('ROWHEIGHT',     (0,0), (-1,-1), 75),
    ]))

    # ── Row 4: Table headers ──────────────────────────────────────────────────
    cw45 = [usable_w * 0.62, usable_w * 0.38]
    t4   = Table([[
        p('დასახელება', bold=True, align='CENTER', color=colors.white, size=11),
        p('ღირებულება<br/>(დღგ-ს ჩათვლით)', bold=True, align='CENTER', color=colors.white, size=11, leading=16),
    ]], colWidths=cw45)
    t4.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), PURPLE),
        ('GRID',          (0,0), (-1,-1), 0.5, colors.white),
        ('TOPPADDING',    (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
    ]))

    # ── Row 5: Service + amount ───────────────────────────────────────────────
    service = (
        'მომსახურების გაწევა და უძრავი ქონების დროებით<br/>'
        'სარგებლობაში გადაცემა (თანმდევი სერვისებით<br/>'
        'და მომსახურებით)'
    )
    t5 = Table([[
        p(service, leading=17, size=11),
        p(invoice.amount + ' ₾', size=13, bold=False, align='CENTER'),
    ]], colWidths=cw45)
    t5.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), LIGHT),
        ('GRID',          (0,0), (-1,-1), 0.5, colors.HexColor('#A8C8E8')),
        ('TOPPADDING',    (0,0), (-1,-1), 14),
        ('BOTTOMPADDING', (0,0), (-1,-1), 14),
        ('LEFTPADDING',   (0,0), (0,-1),  10),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('ROWHEIGHT',     (0,0), (-1,-1), 65),
    ]))

    # ── Row 6: შემსრულებელი label ─────────────────────────────────────────────
    t6 = Table([[p('შემსრულებელი:', bold=True, size=11)]], colWidths=[usable_w])
    t6.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), LIGHT),
        ('TOPPADDING',    (0,0), (-1,-1), 7),
        ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING',   (0,0), (-1,-1), 10),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
    ]))

    # ── Row 7: Executor info + signature ──────────────────────────────────────
    executor = (
        '<b>შპს დემიქსი</b> &nbsp; ს/კ 405328998<br/>'
        'ქინძმარაულის ქუჩა #17<br/>'
        'ტელ: 599 787 453<br/>'
        'მეილი: info@demix.ge<br/>'
        'ბანკი: JSC &quot;Bank of Georgia&quot;<br/>'
        'Bank code: BAGAGE22<br/>'
        'A/A: GE30BG0000000161105533'
    )
    cw7     = [usable_w * 0.62, usable_w * 0.38]
    # Stack director text and signature in same cell with a nested table
    sig_img = Image(SIG_PATH, width=6*cm, height=6*cm) if os.path.exists(SIG_PATH) else p('')
    right_cell = Table([
        [p('დირექტორი<br/>გიორგი გოგოლაძე', size=10, leading=15, align='CENTER')],
        [sig_img],
    ], colWidths=[usable_w * 0.38])
    right_cell.setStyle(TableStyle([
        ('ALIGN',         (0,0), (-1,-1), 'CENTER'),
        ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING',    (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
        ('LEFTPADDING',   (0,0), (-1,-1), 0),
        ('RIGHTPADDING',  (0,0), (-1,-1), 0),
    ]))

    t7 = Table([[p(executor, size=10, leading=15), right_cell]], colWidths=cw7)
    t7.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), colors.white),
        ('BOX',           (0,0), (-1,-1), 0.5, GRAY),
        ('LINEAFTER',     (0,0), (0,-1),  0.5, GRAY),
        ('TOPPADDING',    (0,0), (-1,-1), 12),
        ('BOTTOMPADDING', (0,0), (-1,-1), 12),
        ('LEFTPADDING',   (0,0), (0,0),   10),
        ('VALIGN',        (0,0), (-1,-1), 'TOP'),
    ]))

    doc.build([t1, t2, t3, t4, t5, t6, t7])
    buf.seek(0)
    return buf.read()

# ── Helper ────────────────────────────────────────────────────────────────────

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
    d  = request.get_json()
    co = Company(name=d['name'].strip(), vat=d['vat'].strip(),
                 address=d['address'].strip(), email=d['email'].strip())
    db.session.add(co)
    db.session.commit()
    return jsonify(co.to_dict()), 201

@app.route('/api/companies/<int:cid>', methods=['PUT'])
def update_company(cid):
    co = Company.query.get_or_404(cid)
    d  = request.get_json()
    co.name = d['name'].strip(); co.vat = d['vat'].strip()
    co.address = d['address'].strip(); co.email = d['email'].strip()
    db.session.commit()
    return jsonify(co.to_dict())

@app.route('/api/companies/<int:cid>', methods=['DELETE'])
def delete_company(cid):
    db.session.delete(Company.query.get_or_404(cid))
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/invoice-number', methods=['GET'])
def get_invoice_number():
    return jsonify({'number': next_invoice_number()})

@app.route('/api/invoices/generate', methods=['POST'])
def generate_invoice():
    d       = request.get_json()
    company = Company.query.get_or_404(d['company_id'])
    custom_date = d.get('date', '').strip()
    inv     = Invoice(number=next_invoice_number(), company_id=company.id,
                      amount=str(d['amount']).strip(),
                      custom_date=custom_date if custom_date else None)
    db.session.add(inv)
    db.session.commit()
    pdf_b64 = base64.b64encode(build_pdf(inv)).decode()
    return jsonify({'invoice_id': inv.id, 'invoice_number': inv.number, 'pdf': pdf_b64})

@app.route('/api/invoices/<int:inv_id>/send', methods=['POST'])
def send_invoice(inv_id):
    inv = Invoice.query.get_or_404(inv_id)
    if inv.sent:
        return jsonify({'error': 'Already sent'}), 400

    pdf_bytes = build_pdf(inv)
    tmp_path  = os.path.join(BASE_DIR, 'demix-invoice-%d.pdf' % inv.number)
    with open(tmp_path, 'wb') as f:
        f.write(pdf_bytes)

    try:
        import win32com.client as win32
        import pythoncom
        pythoncom.CoInitialize()
        outlook       = win32.Dispatch('outlook.application')
        mail          = outlook.CreateItem(0)
        # Find info@sawkobi.ge account — fail hard if not found
        sender_email = 'info@sawkobi.ge'
        mapi = outlook.GetNamespace('MAPI')
        sender_account = None
        for i in range(1, mapi.Accounts.Count + 1):
            acc = mapi.Accounts.Item(i)
            if acc.SmtpAddress.lower() == sender_email.lower():
                sender_account = acc
                break
        if not sender_account:
            inv.send_error = 'Account info@sawkobi.ge not found in Outlook'
            db.session.commit()
            return jsonify({'error': 'Account info@sawkobi.ge not found in Outlook. Please add this account and try again.'}), 400
        mail.To       = '; '.join([e.strip() for e in inv.company.email.split(',') if e.strip()])
        mail.Subject  = 'Invoice N%d - შპს დემიქსი' % inv.number
        # Force the correct sender account
        mail._oleobj_.Invoke(*(64209, 0, 8, 0, sender_account))

        # Let Outlook handle the signature properly using Display+Inspector
        import time
        our_text = '<p style="font-family:Calibri,sans-serif;font-size:11pt;">გთხოვთ იხილოთ თანდართული ინვოისი N%d.</p>' % inv.number

        # Display the mail so Outlook loads the correct signature for the account
        mail.Display(False)
        time.sleep(1.5)

        # Get the inspector window and grab the current HTMLBody (has signature)
        inspector = mail.GetInspector
        existing_html = mail.HTMLBody or ''

        # Prepend our text to the existing body (which includes the signature with images)
        mail.HTMLBody = our_text + existing_html

        attachment = mail.Attachments.Add(os.path.abspath(tmp_path))
        attachment.DisplayName = 'demix-invoice-%d.pdf' % inv.number
        mail.Send()
        inv.sent = True
        inv.send_error = None
        db.session.commit()
        return jsonify({'success': True, 'message': 'Sent to %s' % inv.company.email})
    except ImportError:
        return jsonify({'error': 'pywin32 not installed. Run: pip install pywin32'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        try:
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:
            pass
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.route('/api/invoices', methods=['GET'])
def get_invoices():
    return jsonify([i.to_dict() for i in Invoice.query.order_by(Invoice.generated_at.desc()).all()])

@app.route('/api/invoices/<int:inv_id>', methods=['DELETE'])
def delete_invoice(inv_id):
    inv = Invoice.query.get_or_404(inv_id)
    if inv.sent:
        return jsonify({'error': 'Cannot delete a sent invoice'}), 400
    db.session.delete(inv)
    db.session.commit()
    return jsonify({'success': True})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Add send_error column if it doesn't exist yet (for existing databases)
        import sqlalchemy
        with db.engine.connect() as conn:
            for col, typ in [('send_error', 'VARCHAR(255)'), ('custom_date', 'VARCHAR(20)')]:
                try:
                    conn.execute(sqlalchemy.text(f'ALTER TABLE invoice ADD COLUMN {col} {typ}'))
                    conn.commit()
                except Exception:
                    pass
    app.run(debug=True)

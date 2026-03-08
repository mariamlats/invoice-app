from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Image
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os, io, json, base64, sys, hashlib, secrets
from functools import wraps

# ── Load .env ─────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    if getattr(sys, 'frozen', False):
        _env_path = os.path.join(os.path.dirname(sys.executable), '.env')
    else:
        _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    load_dotenv(_env_path)
except ImportError:
    pass

app = Flask(__name__)

_db_url = os.environ.get('DATABASE_URL')
if not _db_url:
    raise RuntimeError('DATABASE_URL not set.')
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

db = SQLAlchemy(app)

# ── Supabase Storage ──────────────────────────────────────────────────────────
SUPABASE_URL         = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
STORAGE_BUCKET       = 'signatures'

def upload_signature_to_supabase(file_bytes: bytes, filename: str, tenant_id: int) -> str:
    """Upload signature to Supabase Storage, return public path key."""
    import urllib.request, urllib.error
    path     = f'tenant_{tenant_id}/{filename}'
    url      = f'{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{path}'
    ext      = filename.rsplit('.', 1)[-1].lower()
    mimetype = 'image/jpeg' if ext in ('jpg', 'jpeg') else 'image/png'
    req  = urllib.request.Request(url, data=file_bytes, method='POST')
    req.add_header('Authorization', f'Bearer {SUPABASE_SERVICE_KEY}')
    req.add_header('Content-Type', mimetype)
    req.add_header('x-upsert', 'true')
    try:
        urllib.request.urlopen(req)
        return path
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Supabase upload failed: {e.code} {e.read().decode()}')

def get_signature_bytes(storage_path: str):
    """Download signature bytes from Supabase Storage (private bucket via service key)."""
    import urllib.request, urllib.error
    url = f'{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{storage_path}'
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {SUPABASE_SERVICE_KEY}')
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except Exception:
        return None

# ── Paths & Fonts ─────────────────────────────────────────────────────────────
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

# ── Password hashing (no bcrypt dependency) ───────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h    = hashlib.sha256((salt + password).encode()).hexdigest()
    return f'{salt}:{h}'

def check_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(':', 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except Exception:
        return False

# ── Models ────────────────────────────────────────────────────────────────────

class Tenant(db.Model):
    __tablename__ = 'tenant'
    id                   = db.Column(db.Integer, primary_key=True)
    name                 = db.Column(db.String(255), nullable=False)
    # Invoice footer fields
    footer_name          = db.Column(db.String(255), nullable=True)
    footer_vat           = db.Column(db.String(100), nullable=True)
    footer_address       = db.Column(db.String(500), nullable=True)
    footer_phone         = db.Column(db.String(100), nullable=True)
    footer_email         = db.Column(db.String(255), nullable=True)
    footer_bank          = db.Column(db.String(255), nullable=True)
    footer_bank_code     = db.Column(db.String(100), nullable=True)
    footer_iban          = db.Column(db.String(100), nullable=True)
    footer_director      = db.Column(db.String(255), nullable=True)
    signature_path       = db.Column(db.String(500), nullable=True)
    invoice_prefix       = db.Column(db.Integer, nullable=False, default=30000)
    # SMTP settings
    smtp_email           = db.Column(db.String(255), nullable=True)
    smtp_password        = db.Column(db.String(500), nullable=True)
    smtp_host            = db.Column(db.String(255), nullable=True)
    smtp_port            = db.Column(db.Integer, nullable=True, default=465)
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'footer_name': self.footer_name, 'footer_vat': self.footer_vat,
            'footer_address': self.footer_address, 'footer_phone': self.footer_phone,
            'footer_email': self.footer_email, 'footer_bank': self.footer_bank,
            'footer_bank_code': self.footer_bank_code, 'footer_iban': self.footer_iban,
            'footer_director': self.footer_director,
            'has_signature': bool(self.signature_path),
            'invoice_prefix': self.invoice_prefix,
            'smtp_email': self.smtp_email or '',
            'smtp_host': self.smtp_host or '',
            'smtp_port': self.smtp_port or 465,
            'smtp_configured': bool(self.smtp_email and self.smtp_password),
        }


class User(db.Model):
    __tablename__ = 'user'
    id            = db.Column(db.Integer, primary_key=True)
    tenant_id     = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False)
    email         = db.Column(db.String(255), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role          = db.Column(db.String(20), nullable=False, default='user')  # 'superadmin' | 'user'
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    tenant        = db.relationship('Tenant', backref='users')


class Company(db.Model):
    __tablename__ = 'company'
    id         = db.Column(db.Integer, primary_key=True)
    tenant_id  = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False)
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
    __tablename__ = 'product'
    id         = db.Column(db.Integer, primary_key=True)
    tenant_id  = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False)
    name       = db.Column(db.String(255), nullable=False)
    unit       = db.Column(db.String(50), nullable=False)
    price      = db.Column(db.String(50), nullable=False)
    vat        = db.Column(db.String(5), nullable=False, default='no')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'unit': self.unit,
                'price': self.price, 'vat': self.vat}


class Invoice(db.Model):
    __tablename__ = 'invoice'
    id           = db.Column(db.Integer, primary_key=True)
    tenant_id    = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False)
    number       = db.Column(db.Integer, nullable=False)
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
            'id': self.id, 'number': self.number,
            'company': self.company.to_dict() if self.company else None,
            'amount': self.amount,
            'items':  json.loads(self.items) if self.items else [],
            'sent':   self.sent,
            'error':  self.send_error,
            'date':   self.custom_date or self.generated_at.strftime('%d/%m/%Y'),
        }

# ── Auth helpers ──────────────────────────────────────────────────────────────

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return db.session.get(User, uid)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            if request.is_json:
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

def get_tenant_id():
    """Return the tenant_id to filter by. Superadmin uses query param ?tenant=X."""
    u = current_user()
    if u.role == 'superadmin':
        tid = request.args.get('tenant') or (request.get_json(silent=True) or {}).get('tenant_id')
        if tid:
            return int(tid)
        return None   # superadmin with no filter = all
    return u.tenant_id

def tenant_filter(query, model, allow_all=False):
    """Apply tenant_id filter to a SQLAlchemy query."""
    u = current_user()
    if u.role == 'superadmin' and allow_all:
        tid = request.args.get('tenant')
        if tid:
            return query.filter(model.tenant_id == int(tid))
        return query  # all tenants
    return query.filter(model.tenant_id == u.tenant_id)

# ── PDF Builder ───────────────────────────────────────────────────────────────

def build_pdf(invoice, show_details=True):
    tenant = db.session.get(Tenant, invoice.tenant_id)
    buf    = io.BytesIO()
    W, H   = A4
    mg     = 1.5 * cm

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

    # Title bar — use tenant name
    title_name = tenant.footer_name or tenant.name if tenant else 'Invoice'
    t1 = Table([[p(f'{title_name} &nbsp;&nbsp; ინვოისი N {invoice.number}',
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
        total_row = [p(''),p(''),p(''),p(''), p('სულ:', bold=True, align='RIGHT', size=11), p(str(invoice.amount) + ' GEL', bold=True, align='CENTER', size=11)]
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
        total_row = [p(''), p('სულ:', bold=True, align='RIGHT', size=11), p(str(invoice.amount) + ' GEL', bold=True, align='CENTER', size=11)]

    rows = [hdr] + data_rows + [total_row]
    nr   = len(rows)
    st   = [
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

    # Build executor text from tenant fields
    if tenant:
        lines = []
        if tenant.footer_name:    lines.append(f'<b>{tenant.footer_name}</b>')
        if tenant.footer_vat:     lines.append(f'ს/კ {tenant.footer_vat}')
        if tenant.footer_address: lines.append(tenant.footer_address)
        if tenant.footer_phone:   lines.append(f'ტელ: {tenant.footer_phone}')
        if tenant.footer_email:   lines.append(f'მეილი: {tenant.footer_email}')
        if tenant.footer_bank:    lines.append(f'ბანკი: {tenant.footer_bank}')
        if tenant.footer_bank_code: lines.append(f'Bank code: {tenant.footer_bank_code}')
        if tenant.footer_iban:    lines.append(f'A/A: {tenant.footer_iban}')
        executor = '<br/>'.join(lines)
        director = tenant.footer_director or ''
    else:
        executor = ''
        director = ''

    # Signature from Supabase
    sig_img = p('')
    if tenant and tenant.signature_path:
        sig_bytes = get_signature_bytes(tenant.signature_path)
        if sig_bytes:
            sig_buf = io.BytesIO(sig_bytes)
            sig_img = Image(sig_buf, width=6*cm, height=6*cm)

    rc = Table([
        [p(f'დირექტორი<br/>{director}', size=10, leading=15, align='CENTER')],
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

def next_invoice_number(tenant_id):
    last = db.session.query(db.func.max(Invoice.number)).filter(Invoice.tenant_id == tenant_id).scalar()
    if last:
        return last + 1
    t = db.session.get(Tenant, tenant_id)
    return (t.invoice_prefix + 1) if t else 30001

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET'])
def login_page():
    if current_user():
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/api/auth/login', methods=['POST'])
def do_login():
    d     = request.get_json()
    email = (d.get('email') or '').strip().lower()
    pwd   = d.get('password') or ''
    user  = User.query.filter_by(email=email).first()
    if not user or not check_password(pwd, user.password_hash):
        return jsonify({'error': 'არასწორი მეილი ან პაროლი'}), 401
    session.clear()
    session['user_id']   = user.id
    session['tenant_id'] = user.tenant_id
    session['role']      = user.role
    return jsonify({'ok': True, 'role': user.role, 'tenant_name': user.tenant.name})

@app.route('/api/auth/logout', methods=['POST'])
def do_logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/auth/me', methods=['GET'])
@login_required
def me():
    u = current_user()
    return jsonify({'email': u.email, 'role': u.role, 'tenant_name': u.tenant.name, 'tenant_id': u.tenant_id})

# ── Main app page ─────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return render_template('index.html')

# ── Superadmin: tenant management ─────────────────────────────────────────────

@app.route('/api/admin/tenants', methods=['GET'])
@login_required
def admin_list_tenants():
    if current_user().role != 'superadmin':
        return jsonify({'error': 'Forbidden'}), 403
    tenants = Tenant.query.order_by(Tenant.name).all()
    result  = []
    for t in tenants:
        u = User.query.filter_by(tenant_id=t.id).first()
        result.append({**t.to_dict(), 'email': u.email if u else ''})
    return jsonify(result)

@app.route('/api/admin/tenants', methods=['POST'])
@login_required
def admin_create_tenant():
    if current_user().role != 'superadmin':
        return jsonify({'error': 'Forbidden'}), 403
    d     = request.get_json()
    email = (d.get('email') or '').strip().lower()
    pwd   = (d.get('password') or '').strip()
    name  = (d.get('name') or '').strip()
    if not email or not pwd or not name:
        return jsonify({'error': 'სახელი, მეილი და პაროლი სავალდებულოა'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'ეს მეილი უკვე გამოყენებულია'}), 400
    tenant = Tenant(name=name)
    db.session.add(tenant)
    db.session.flush()
    user = User(tenant_id=tenant.id, email=email,
                password_hash=hash_password(pwd), role='user')
    db.session.add(user)
    db.session.commit()
    return jsonify({'ok': True, 'tenant_id': tenant.id}), 201

@app.route('/api/admin/tenants/<int:tid>', methods=['DELETE'])
@login_required
def admin_delete_tenant(tid):
    if current_user().role != 'superadmin':
        return jsonify({'error': 'Forbidden'}), 403
    tenant = db.session.get(Tenant, tid)
    if not tenant:
        return jsonify({'error': 'Not found'}), 404
    # cascade delete
    Invoice.query.filter_by(tenant_id=tid).delete()
    Company.query.filter_by(tenant_id=tid).delete()
    Product.query.filter_by(tenant_id=tid).delete()
    User.query.filter_by(tenant_id=tid).delete()
    db.session.delete(tenant)
    db.session.commit()
    return jsonify({'ok': True})

# ── Settings (tenant profile) ─────────────────────────────────────────────────

@app.route('/api/settings', methods=['GET'])
@login_required
def get_settings():
    u = current_user()
    t = db.session.get(Tenant, u.tenant_id)
    return jsonify(t.to_dict())

@app.route('/api/settings', methods=['PUT'])
@login_required
def save_settings():
    u = current_user()
    t = db.session.get(Tenant, u.tenant_id)
    d = request.get_json()
    t.footer_name      = d.get('footer_name', t.footer_name)
    t.footer_vat       = d.get('footer_vat', t.footer_vat)
    t.footer_address   = d.get('footer_address', t.footer_address)
    t.footer_phone     = d.get('footer_phone', t.footer_phone)
    t.footer_email     = d.get('footer_email', t.footer_email)
    t.footer_bank      = d.get('footer_bank', t.footer_bank)
    t.footer_bank_code = d.get('footer_bank_code', t.footer_bank_code)
    t.footer_iban      = d.get('footer_iban', t.footer_iban)
    t.footer_director  = d.get('footer_director', t.footer_director)
    if d.get('invoice_prefix'):
        t.invoice_prefix = int(d['invoice_prefix'])
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/settings/smtp', methods=['PUT'])
@login_required
def save_smtp():
    u = current_user()
    t = db.session.get(Tenant, u.tenant_id)
    d = request.get_json()
    t.smtp_email    = (d.get('smtp_email') or '').strip()
    t.smtp_host     = (d.get('smtp_host') or '').strip()
    t.smtp_port     = int(d.get('smtp_port') or 465)
    # Only update password if a new one was provided
    new_pwd = (d.get('smtp_password') or '').strip()
    if new_pwd:
        t.smtp_password = new_pwd
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/settings/smtp/test', methods=['POST'])
@login_required
def test_smtp():
    u = current_user()
    t = db.session.get(Tenant, u.tenant_id)
    if not t.smtp_email or not t.smtp_password or not t.smtp_host:
        return jsonify({'error': 'SMTP კონფიგურაცია არ არის შევსებული'}), 400
    try:
        import smtplib
        connected = False
        last_err  = ''
        for try_port, try_ssl in [(t.smtp_port or 465, True), (587, False)]:
            try:
                if try_ssl:
                    srv = smtplib.SMTP_SSL(t.smtp_host, try_port, timeout=10)
                else:
                    srv = smtplib.SMTP(t.smtp_host, try_port, timeout=10)
                    srv.ehlo(); srv.starttls(); srv.ehlo()
                srv.login(t.smtp_email, t.smtp_password)
                srv.quit()
                connected = True
                break
            except Exception as ex:
                last_err = str(ex)
                continue
        if not connected:
            return jsonify({'error': last_err}), 400
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/settings/signature', methods=['POST'])
@login_required
def upload_signature():
    u = current_user()
    t = db.session.get(Tenant, u.tenant_id)
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f         = request.files['file']
    ext       = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg'):
        return jsonify({'error': 'PNG ან JPEG ფაილი სავალდებულოა'}), 400
    file_bytes = f.read()
    if len(file_bytes) > 2 * 1024 * 1024:
        return jsonify({'error': 'ფაილი 2MB-ზე მეტია'}), 400
    try:
        path = upload_signature_to_supabase(file_bytes, f'signature{ext}', u.tenant_id)
        t.signature_path = path
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Companies ─────────────────────────────────────────────────────────────────

@app.route('/api/companies', methods=['GET'])
@login_required
def get_companies():
    u  = current_user()
    q  = Company.query.filter_by(tenant_id=u.tenant_id).order_by(Company.name)
    return jsonify([c.to_dict() for c in q.all()])

@app.route('/api/companies', methods=['POST'])
@login_required
def create_company():
    u  = current_user()
    d  = request.get_json()
    co = Company(tenant_id=u.tenant_id,
                 name=d['name'].strip(), vat=d['vat'].strip(),
                 address=d['address'].strip(), email=d['email'].strip(),
                 legal_form=d.get('legal_form','შპს'))
    db.session.add(co); db.session.commit()
    return jsonify(co.to_dict()), 201

@app.route('/api/companies/<int:cid>', methods=['PUT'])
@login_required
def update_company(cid):
    u  = current_user()
    co = Company.query.filter_by(id=cid, tenant_id=u.tenant_id).first_or_404()
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
@login_required
def delete_company(cid):
    u  = current_user()
    co = Company.query.filter_by(id=cid, tenant_id=u.tenant_id).first_or_404()
    for inv in co.invoices:
        db.session.delete(inv)
    db.session.delete(co)
    db.session.commit()
    return jsonify({'success': True})

# ── Products ──────────────────────────────────────────────────────────────────

@app.route('/api/products', methods=['GET'])
@login_required
def get_products():
    u = current_user()
    return jsonify([p.to_dict() for p in Product.query.filter_by(tenant_id=u.tenant_id).order_by(Product.name).all()])

@app.route('/api/products', methods=['POST'])
@login_required
def create_product():
    u  = current_user()
    d  = request.get_json()
    pr = Product(tenant_id=u.tenant_id,
                 name=d['name'].strip(), unit=d['unit'].strip(),
                 price=d.get('price','').strip() or '0', vat=d.get('vat','no'))
    db.session.add(pr); db.session.commit()
    return jsonify(pr.to_dict()), 201

@app.route('/api/products/<int:pid>', methods=['PUT'])
@login_required
def update_product(pid):
    u  = current_user()
    pr = Product.query.filter_by(id=pid, tenant_id=u.tenant_id).first_or_404()
    d  = request.get_json()
    pr.name=d['name'].strip(); pr.unit=d['unit'].strip()
    pr.price=d.get('price','').strip() or '0'; pr.vat=d.get('vat','no')
    db.session.commit()
    return jsonify(pr.to_dict())

@app.route('/api/products/<int:pid>', methods=['DELETE'])
@login_required
def delete_product(pid):
    u  = current_user()
    pr = Product.query.filter_by(id=pid, tenant_id=u.tenant_id).first_or_404()
    db.session.delete(pr); db.session.commit()
    return jsonify({'success': True})

# ── Invoices ──────────────────────────────────────────────────────────────────

@app.route('/api/invoice-number', methods=['GET'])
@login_required
def get_invoice_number():
    u = current_user()
    return jsonify({'number': next_invoice_number(u.tenant_id)})

@app.route('/api/invoices/generate', methods=['POST'])
@login_required
def generate_invoice():
    try:
        u           = current_user()
        d           = request.get_json()
        company     = Company.query.filter_by(id=d['company_id'], tenant_id=u.tenant_id).first_or_404()
        custom_date = d.get('date', '').strip()
        items       = d.get('items', [])
        total = 0.0
        for item in items:
            try: total += float(str(item.get('total','0')).replace(',','.'))
            except: pass
        amount_str   = ('%.2f' % total) if total else '0.00'
        show_details = d.get('show_details', True)
        inv = Invoice(
            tenant_id   = u.tenant_id,
            number      = next_invoice_number(u.tenant_id),
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
@login_required
def send_invoice(inv_id):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email.mime.text import MIMEText
    from email import encoders

    u   = current_user()
    inv = Invoice.query.filter_by(id=inv_id, tenant_id=u.tenant_id).first_or_404()
    if inv.sent:
        return jsonify({'error': 'Already sent'}), 400

    tenant = db.session.get(Tenant, u.tenant_id)
    if not tenant.smtp_email or not tenant.smtp_password or not tenant.smtp_host:
        return jsonify({'error': 'გთხოვთ ჯერ შეავსოთ ელ. ფოსტის პარამეტრები (პარამეტრები → ელ. ფოსტა)'}), 400

    pdf_bytes  = build_pdf(inv)
    recipients = [e.strip() for e in inv.company.email.split(',') if e.strip()]

    try:
        msg = MIMEMultipart()
        msg['From']    = tenant.smtp_email
        msg['To']      = ', '.join(recipients)
        msg['Subject'] = f'ინვოისი N{inv.number} - {tenant.footer_name or tenant.name}'

        body = MIMEText(f'გთხოვთ იხილოთ თანდართული ინვოისი N{inv.number}.', 'plain', 'utf-8')
        msg.attach(body)

        part = MIMEBase('application', 'octet-stream')
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="invoice-{inv.number}.pdf"')
        msg.attach(part)

        host = tenant.smtp_host
        port = tenant.smtp_port or 465
        sent = False

        # Try SSL first (port 465), then STARTTLS (port 587)
        for try_port, try_ssl in [(port, True), (587, False)]:
            try:
                if try_ssl:
                    srv = smtplib.SMTP_SSL(host, try_port, timeout=15)
                else:
                    srv = smtplib.SMTP(host, try_port, timeout=15)
                    srv.ehlo()
                    srv.starttls()
                    srv.ehlo()
                srv.login(tenant.smtp_email, tenant.smtp_password)
                srv.sendmail(tenant.smtp_email, recipients, msg.as_string())
                srv.quit()
                sent = True
                break
            except Exception:
                continue

        if not sent:
            raise Exception('SMTP კავშირი ვერ დამყარდა. შეამოწმეთ სერვერი და პაროლი.')

        inv.sent = True; inv.send_error = None
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        inv.send_error = str(e)
        db.session.commit()
        return jsonify({'error': str(e)}), 500

@app.route('/api/invoices', methods=['GET'])
@login_required
def get_invoices():
    u = current_user()
    return jsonify([i.to_dict() for i in Invoice.query.filter_by(tenant_id=u.tenant_id).order_by(Invoice.generated_at.desc()).all()])

@app.route('/api/invoices/<int:inv_id>', methods=['DELETE'])
@login_required
def delete_invoice(inv_id):
    u   = current_user()
    inv = Invoice.query.filter_by(id=inv_id, tenant_id=u.tenant_id).first_or_404()
    if inv.sent:
        return jsonify({'error': 'Cannot delete a sent invoice'}), 400
    db.session.delete(inv); db.session.commit()
    return jsonify({'success': True})

# ── Superadmin data views ─────────────────────────────────────────────────────

@app.route('/api/auth/change-password', methods=['POST'])
@login_required
def change_password():
    u   = current_user()
    d   = request.get_json()
    cur = d.get('current_password', '')
    new = d.get('new_password', '')
    if not check_password(cur, u.password_hash):
        return jsonify({'error': 'მიმდინარე პაროლი არასწორია'}), 400
    if len(new) < 6:
        return jsonify({'error': 'პაროლი მინიმუმ 6 სიმბოლო უნდა იყოს'}), 400
    u.password_hash = hash_password(new)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/auth/change-email', methods=['POST'])
@login_required
def change_email():
    u     = current_user()
    d     = request.get_json()
    cur   = d.get('current_password', '')
    email = (d.get('new_email') or '').strip().lower()
    if not check_password(cur, u.password_hash):
        return jsonify({'error': 'მიმდინარე პაროლი არასწორია'}), 400
    if len(email) < 3:
        return jsonify({'error': 'მინიმუმ 3 სიმბოლო'}), 400
    if User.query.filter(User.email == email, User.id != u.id).first():
        return jsonify({'error': 'ეს მეილი უკვე გამოყენებულია'}), 400
    u.email = email
    db.session.commit()
    session['user_id'] = u.id  # refresh session
    return jsonify({'ok': True, 'new_email': email})

@app.route('/api/admin/tenants/<int:tid>/reset-password', methods=['POST'])
@login_required
def admin_reset_password(tid):
    if current_user().role != 'superadmin':
        return jsonify({'error': 'Forbidden'}), 403
    d   = request.get_json()
    new = d.get('new_password', '')
    if len(new) < 6:
        return jsonify({'error': 'პაროლი მინიმუმ 6 სიმბოლო უნდა იყოს'}), 400
    u = User.query.filter_by(tenant_id=tid).first()
    if not u:
        return jsonify({'error': 'User not found'}), 404
    u.password_hash = hash_password(new)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/admin/tenants/<int:tid>/reset-email', methods=['POST'])
@login_required
def admin_reset_email(tid):
    if current_user().role != 'superadmin':
        return jsonify({'error': 'Forbidden'}), 403
    d     = request.get_json()
    email = (d.get('new_email') or '').strip().lower()
    if len(email) < 3:
        return jsonify({'error': 'მინიმუმ 3 სიმბოლო'}), 400
    if User.query.filter(User.email == email, User.tenant_id != tid).first():
        return jsonify({'error': 'ეს მეილი უკვე გამოყენებულია'}), 400
    u = User.query.filter_by(tenant_id=tid).first()
    if not u:
        return jsonify({'error': 'User not found'}), 404
    u.email = email
    db.session.commit()
    return jsonify({'ok': True})
    if not u:
        return jsonify({'error': 'User not found'}), 404
    u.password_hash = hash_password(new)
    db.session.commit()
    return jsonify({'ok': True})


@login_required
def admin_all_invoices():
    if current_user().role != 'superadmin':
        return jsonify({'error': 'Forbidden'}), 403
    tid = request.args.get('tenant')
    q   = Invoice.query.order_by(Invoice.generated_at.desc())
    if tid:
        q = q.filter_by(tenant_id=int(tid))
    return jsonify([i.to_dict() for i in q.all()])

# ── Migrations ────────────────────────────────────────────────────────────────

def run_migrations():
    import sqlalchemy
    migrations = [
        ('tenant', 'smtp_email',    'VARCHAR(255)'),
        ('tenant', 'smtp_password', 'VARCHAR(500)'),
        ('tenant', 'smtp_host',     'VARCHAR(255)'),
        ('tenant', 'smtp_port',     'INTEGER DEFAULT 465'),
        ('invoice', 'send_error',  'VARCHAR(255)'),
        ('invoice', 'custom_date', 'VARCHAR(20)'),
        ('invoice', 'items',       'TEXT'),
        ('invoice', 'tenant_id',   'INTEGER'),
        ('company', 'legal_form',  "VARCHAR(20) DEFAULT 'შპს'"),
        ('company', 'status',      "VARCHAR(20) DEFAULT 'active'"),
        ('company', 'tenant_id',   'INTEGER'),
        ('product', 'vat',         "VARCHAR(5) DEFAULT 'no'"),
        ('product', 'tenant_id',   'INTEGER'),
    ]
    for table, col, typ in migrations:
        try:
            with db.engine.connect() as conn:
                conn.execute(sqlalchemy.text(
                    'ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s' % (table, col, typ)
                ))
                conn.commit()
        except Exception:
            pass

def ensure_superadmin():
    """Create superadmin tenant + user if none exists."""
    if User.query.filter_by(role='superadmin').first():
        return
    sa_email = os.environ.get('SUPERADMIN_EMAIL', 'admin@invoiceapp.com')
    sa_pwd   = os.environ.get('SUPERADMIN_PASSWORD', 'changeme123')
    t = Tenant(name='Super Admin', invoice_prefix=90000)
    db.session.add(t); db.session.flush()
    u = User(tenant_id=t.id, email=sa_email,
             password_hash=hash_password(sa_pwd), role='superadmin')
    db.session.add(u); db.session.commit()
    print(f'[INIT] Superadmin created: {sa_email} / {sa_pwd}')

with app.app_context():
    db.create_all()
    run_migrations()
    ensure_superadmin()

if __name__ == '__main__':
    app.run(debug=True)

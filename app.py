from flask import Flask, render_template, request, jsonify, session, redirect, url_for, abort
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape as xml_escape
from urllib.parse import urlsplit
from urllib import request as urllib_request
from werkzeug.exceptions import HTTPException
from sqlalchemy.exc import IntegrityError
from PIL import Image as PILImage, UnidentifiedImageError
import click
import warnings
import re
from security import (hash_password, check_password, session_stamp, password_is_valid,
                      LEGACY_HASH, encrypt_credential, decrypt_credential,
                      smtp_destination, open_smtp)
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Image
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os, io, json, base64, sys, hashlib, secrets, hmac
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
_secret_key = os.environ.get('SECRET_KEY', '')
if len(_secret_key) < 32:
    raise RuntimeError('Set SECRET_KEY to a persistent random value of at least 32 characters.')
app.config['SECRET_KEY'] = _secret_key
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Disable secure cookies only for an explicitly configured loopback desktop session.
_local_http = os.environ.get('LOCAL_HTTP', '').lower() == 'true'
app.config['SESSION_COOKIE_SECURE'] = not _local_http
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['MAX_CONTENT_LENGTH'] = 3 * 1024 * 1024
app.config['MAX_FORM_MEMORY_SIZE'] = 128 * 1024
app.config['MAX_FORM_PARTS'] = 10
app.config['TRUSTED_HOSTS'] = [h.strip() for h in os.environ.get(
    'TRUSTED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]

db = SQLAlchemy(app)

# ── Supabase Storage ──────────────────────────────────────────────────────────
SUPABASE_URL         = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
STORAGE_BUCKET       = 'signatures'

class NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def storage_url(path):
    parsed = urlsplit(SUPABASE_URL)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise ValueError('Configure a valid HTTPS SUPABASE_URL without a path.')
    if not SUPABASE_SERVICE_KEY:
        raise ValueError('Supabase Storage is not configured.')
    if not re.fullmatch(r'tenant_[1-9][0-9]*/signature\.(png|jpg|jpeg)', path):
        raise ValueError('Invalid signature storage path.')
    return f'{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{path}'


def upload_signature_to_supabase(file_bytes: bytes, filename: str, tenant_id: int) -> str:
    path = f'tenant_{tenant_id}/{filename}'
    req = urllib_request.Request(storage_url(path), data=file_bytes, method='POST')
    req.add_header('Authorization', f'Bearer {SUPABASE_SERVICE_KEY}')
    req.add_header('Content-Type', 'image/png')
    req.add_header('x-upsert', 'true')
    with urllib_request.build_opener(NoRedirect()).open(req, timeout=10):
        pass
    return path


def get_signature_bytes(storage_path: str, tenant_id: int):
    if not storage_path.startswith(f'tenant_{tenant_id}/'):
        return None
    try:
        req = urllib_request.Request(storage_url(storage_path))
        req.add_header('Authorization', f'Bearer {SUPABASE_SERVICE_KEY}')
        with urllib_request.build_opener(NoRedirect()).open(req, timeout=10) as resp:
            content = resp.read(2 * 1024 * 1024 + 1)
        return content if len(content) <= 2 * 1024 * 1024 else None
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
    # Email template
    email_from           = db.Column(db.String(255), nullable=True)
    email_subject        = db.Column(db.String(500), nullable=True)
    email_body           = db.Column(db.Text, nullable=True)
    # English versions
    footer_name_en       = db.Column(db.String(255), nullable=True)
    footer_address_en    = db.Column(db.String(500), nullable=True)
    footer_director_en   = db.Column(db.String(255), nullable=True)
    footer_bank_en       = db.Column(db.String(255), nullable=True)
    email_subject_en     = db.Column(db.String(500), nullable=True)
    email_body_en        = db.Column(db.Text, nullable=True)
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
            'email_from':    self.email_from    or '',
            'email_subject': self.email_subject or '',
            'email_body':    self.email_body    or '',
            'footer_name_en':     self.footer_name_en     or '',
            'footer_address_en':  self.footer_address_en  or '',
            'footer_director_en': self.footer_director_en or '',
            'footer_bank_en':     self.footer_bank_en     or '',
            'email_subject_en':   self.email_subject_en   or '',
            'email_body_en':      self.email_body_en      or '',
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
    language   = db.Column(db.String(2), nullable=False, default='ka')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'legal_form': self.legal_form, 'name': self.name,
                'vat': self.vat, 'address': self.address, 'email': self.email,
                'status': self.status, 'language': self.language or 'ka'}


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

class AuthFailure(db.Model):
    __tablename__ = 'auth_failure'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


def auth_keys(identifier):
    # Hash account identifiers and IPs rather than recording them in this table.
    return [hashlib.sha256(value.encode()).hexdigest() for value in
            ('account:' + identifier, 'ip:' + (request.remote_addr or 'unknown'))]


def auth_attempt_allowed(identifier):
    cutoff = datetime.utcnow() - timedelta(minutes=15)
    AuthFailure.query.filter(AuthFailure.created_at < cutoff).delete()
    db.session.commit()
    return all(AuthFailure.query.filter(AuthFailure.key == key,
                                       AuthFailure.created_at >= cutoff).count() < limit
               for key, limit in zip(auth_keys(identifier), (10, 50)))


def record_auth_failure(identifier):
    for key in auth_keys(identifier):
        db.session.add(AuthFailure(key=key))
    db.session.commit()


def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']


@app.context_processor
def security_template_context():
    return {'csrf_token': csrf_token}


@app.before_request
def protect_requests():
    if _local_http and request.remote_addr not in ('127.0.0.1', '::1'):
        abort(403, 'LOCAL_HTTP is restricted to loopback connections.')
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        supplied = request.headers.get('X-CSRF-Token', '')
        expected = session.get('csrf_token', '')
        if (not re.fullmatch(r'[0-9a-f]{64}', supplied) or not expected
                or not hmac.compare_digest(supplied, expected)):
            abort(403, 'Session expired or invalid CSRF token. Reload the page.')
        if request.is_json:
            data = request.get_json()
            if not isinstance(data, dict):
                abort(400, 'JSON body must be an object.')
            for key, value in data.items():
                limit = 10000 if key in ('email_body', 'email_body_en') else 1024 if 'password' in key else 500 if 'address' in key or key in ('email_subject', 'email_subject_en') else 255
                if isinstance(value, str) and len(value) > limit:
                    abort(400, 'Input exceeds the allowed length.')
                if isinstance(value, (dict, list)) and key != 'items':
                    abort(400, 'Unexpected nested input.')
                if key not in ('items', 'company_id', 'tenant_id', 'invoice_prefix',
                               'smtp_port', 'show_details') and not isinstance(value, str):
                    abort(400, 'Text fields must contain strings.')
        elif request.content_length and request.endpoint != 'upload_signature':
            abort(415, 'Use application/json for API requests.')


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; "
        "frame-src 'self' blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    return response


@app.errorhandler(HTTPException)
def http_error(error):
    db.session.rollback()
    if request.path.startswith('/api/'):
        return jsonify({'error': error.description}), error.code
    return error


@app.errorhandler(ValueError)
@app.errorhandler(TypeError)
@app.errorhandler(KeyError)
def invalid_input(error):
    db.session.rollback()
    return jsonify({'error': 'Invalid input or missing server configuration.'}), 400


@app.errorhandler(IntegrityError)
def integrity_error(error):
    db.session.rollback()
    return jsonify({'error': 'The update conflicts with an existing record.'}), 409


def require_text(data, field, maximum=255):
    value = data.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        abort(400, f'{field} is required and must contain at most {maximum} characters.')
    return value.strip()


def validate_email(value, use_abort=True):
    if (not isinstance(value, str) or len(value) > 255
            or not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', value)):
        if use_abort:
            abort(400, 'Enter a valid email address.')
        raise ValueError('Enter a valid email address.')


def validate_company(data):
    for field, maximum in [('name', 255), ('vat', 100), ('address', 500), ('email', 255)]:
        require_text(data, field, maximum)
    for email in data['email'].split(','):
        validate_email(email.strip())
    if data.get('language', 'ka') not in ('ka', 'en', 'it'):
        abort(400, 'Unsupported invoice language.')
    if data.get('status', 'active') not in ('active', 'passive'):
        abort(400, 'Unsupported company status.')
    if len(data.get('legal_form', '')) > 20:
        abort(400, 'Legal form is too long.')


def decimal_value(value):
    try:
        value = str(value)
        if len(value) > 50:
            raise ValueError
        number = Decimal(value.replace(',', '.'))
        if not number.is_finite() or number < 0 or number > Decimal('1000000000000'):
            raise ValueError
        return number
    except (InvalidOperation, ValueError):
        abort(400, 'Amounts must be finite, non-negative numbers within the supported range.')


def validate_product(data):
    require_text(data, 'name')
    require_text(data, 'unit', 50)
    decimal_value(data.get('price') or '0')
    if data.get('vat', 'no') not in ('yes', 'no'):
        abort(400, 'Unsupported VAT setting.')


def validate_invoice_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        abort(400, 'An invoice must contain between 1 and 200 items.')
    checked, total = [], Decimal('0')
    for item in items:
        if not isinstance(item, dict):
            abort(400, 'Each invoice item must be an object.')
        name = require_text(item, 'name')
        unit = item.get('unit', '')
        if not isinstance(unit, str) or len(unit) > 50:
            abort(400, 'Invalid unit.')
        qty, price = decimal_value(item.get('qty', '0')), decimal_value(item.get('price', '0'))
        line_total = (qty * price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if line_total > Decimal('1000000000000'):
            abort(400, 'Invoice total exceeds the supported range.')
        total += line_total
        checked.append({'name': name, 'unit': unit, 'qty': str(qty),
                        'price': str(price), 'total': format(line_total, '.2f')})
    if total > Decimal('1000000000000'):
        abort(400, 'Invoice total exceeds the supported range.')
    return checked, format(total, '.2f')


def sanitize_signature(content):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', PILImage.DecompressionBombWarning)
            with PILImage.open(io.BytesIO(content)) as image:
                if image.format not in ('PNG', 'JPEG') or image.width * image.height > 4000000:
                    raise ValueError('Signature must be a PNG/JPEG image of at most 4 megapixels.')
                image.load()
                output = io.BytesIO()
                image.convert('RGBA' if 'A' in image.getbands() else 'RGB').save(output, format='PNG')
                clean = output.getvalue()
                if len(clean) > 2 * 1024 * 1024:
                    raise ValueError('Decoded signature exceeds 2 MB.')
                return clean
    except (UnidentifiedImageError, OSError, PILImage.DecompressionBombError,
            PILImage.DecompressionBombWarning) as exc:
        raise ValueError('Upload a valid PNG or JPEG signature.') from exc


def pdf_text(value):
    """Escape database/user content before inserting it into ReportLab markup."""
    return xml_escape('' if value is None else str(value), quote=True)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    user = db.session.get(User, uid)
    expected = session_stamp(user.password_hash, app.secret_key) if user else ''
    if not user or not hmac.compare_digest(session.get('auth_stamp', ''), expected):
        session.clear()
        return None
    return user

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            if request.path.startswith('/api/'):
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
    lang = invoice.company.language or 'ka'
    is_en = lang == 'en'
    is_it = lang == 'it'
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

    def p(text, size=9, bold=False, align='LEFT', color=colors.black, leading=None, markup=False):
        text = str(text) if markup else pdf_text(text).replace('\n', '<br/>')
        return Paragraph(text, ParagraphStyle('x',
            fontName='DejaVuBold' if bold else 'DejaVu',
            fontSize=size, textColor=color,
            alignment={'LEFT':0,'CENTER':1,'RIGHT':2}[align],
            leading=leading or size*1.4))

    # Title bar — use tenant name
    title_name = (tenant.footer_name_en or tenant.footer_name or tenant.name) if (tenant and is_en) else (tenant.footer_name or tenant.name) if tenant else 'Invoice'
    inv_label = "Invoice N" if is_en else ("Fattura N" if is_it else "ინვოისი N")
    t1 = Table([[p(f'{pdf_text(title_name)} &nbsp;&nbsp; {inv_label} {invoice.number}',
                   size=15, bold=True, align='CENTER', color=colors.white, markup=True)]],
               colWidths=[uw])
    t1.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),BLUE),
        ('TOPPADDING',(0,0),(-1,-1),9),('BOTTOMPADDING',(0,0),(-1,-1),9),
        ('LEFTPADDING',(0,0),(-1,-1),10),
    ]))

    # შემკვეთი + date
    t2 = Table([[p('Client:' if is_en else ('Cliente:' if is_it else 'შემკვეთი:'), bold=True, size=11),
                 p(date_str, bold=True, align='RIGHT', size=11)]],
               colWidths=[uw*0.62, uw*0.38])
    t2.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),LIGHT),
        ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),
        ('LEFTPADDING',(0,0),(0,-1),10),('RIGHTPADDING',(1,0),(1,-1),10),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
    ]))

    # Client block
    if is_en:
        lf_en = {'შპს': 'LTD'}.get(c.legal_form, c.legal_form or '')
        full_name = '%s %s' % (lf_en, c.name) if lf_en else c.name
    elif is_it:
        lf_it = {'შპს': 'S.r.l.', 'ი/მ': 'Imp. Ind.', 'ფ/პ': 'Lib. Prof.'}.get(c.legal_form, c.legal_form or '')
        full_name = '%s %s' % (lf_it, c.name) if lf_it else c.name
    else:
        full_name = '%s %s' % (c.legal_form or 'შპს', c.name)
    if is_en:
        ct = '<b>%s</b><br/>ID: %s<br/>Address: %s<br/>Email: %s' % tuple(pdf_text(v) for v in (full_name, c.vat, c.address, c.email))
    elif is_it:
        ct = '<b>%s</b><br/>P.IVA: %s<br/>Indirizzo: %s<br/>Email: %s' % tuple(pdf_text(v) for v in (full_name, c.vat, c.address, c.email))
    else:
        ct = '<b>%s</b><br/>ს/კ: %s<br/>იურიდიული მისამართი: %s<br/>ელ. მეილი: %s' % tuple(pdf_text(v) for v in (full_name, c.vat, c.address, c.email))
    t3 = Table([[p(ct, leading=17, size=11, markup=True)]], colWidths=[uw])
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
            p('Product Name' if is_en else ('Descrizione' if is_it else 'პროდუქტის დასახელება'), bold=True, align='CENTER', color=colors.white, size=10),
            p('Unit' if is_en else ('U.M.' if is_it else 'ზომის\nერთეული'), bold=True, align='CENTER', color=colors.white, size=10),
            p('Qty' if is_en else ('Qtà' if is_it else 'რაოდენობა'), bold=True, align='CENTER', color=colors.white, size=10),
            p('Unit Price' if is_en else ('Prezzo Unit.' if is_it else 'ერთეულის ფასი'), bold=True, align='CENTER', color=colors.white, size=10),
            p('Amount' if is_en else ('Importo' if is_it else 'ღირებულება'), bold=True, align='CENTER', color=colors.white, size=10),
        ]
        data_rows = [[
            p(str(i), align='CENTER', size=10),
            p(item.get('name',''), size=10, leading=14),
            p(item.get('unit',''), align='CENTER', size=10),
            p(str(item.get('qty','')), align='CENTER', size=10),
            p(str(item.get('price','')), align='CENTER', size=10),
            p(str(item.get('total','')), align='CENTER', size=10),
        ] for i, item in enumerate(items, 1)]
        total_row = [p(''),p(''),p(''),p(''), p('Total:' if is_en else ('Totale:' if is_it else 'სულ:'), bold=True, align='RIGHT', size=11), p(str(invoice.amount) + ' €', bold=True, align='CENTER', size=11)]
    else:
        cw = [uw*0.06, uw*0.74, uw*0.20]
        hdr = [
            p('#', bold=True, align='CENTER', color=colors.white, size=10),
            p('Product Name' if is_en else ('Descrizione' if is_it else 'პროდუქტის დასახელება'), bold=True, align='CENTER', color=colors.white, size=10),
            p('Amount' if is_en else ('Importo' if is_it else 'ღირებულება'), bold=True, align='CENTER', color=colors.white, size=10),
        ]
        data_rows = [[
            p(str(i), align='CENTER', size=10),
            p(item.get('name',''), size=10, leading=14),
            p(str(item.get('total','')), align='CENTER', size=10),
        ] for i, item in enumerate(items, 1)]
        total_row = [p(''), p('Total:' if is_en else ('Totale:' if is_it else 'სულ:'), bold=True, align='RIGHT', size=11), p(str(invoice.amount) + ' €', bold=True, align='CENTER', size=11)]

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
    t6 = Table([[p('Executor:' if is_en else ('Fornitore:' if is_it else 'შემსრულებელი:'), bold=True, size=11)]], colWidths=[uw])
    t6.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),LIGHT),
        ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),
        ('LEFTPADDING',(0,0),(-1,-1),10),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
    ]))

    # Build executor text from tenant fields
    if tenant:
        lines = []
        _name    = (tenant.footer_name_en    or tenant.footer_name)    if is_en else tenant.footer_name
        _address = (tenant.footer_address_en or tenant.footer_address) if is_en else tenant.footer_address
        _bank    = (tenant.footer_bank_en    or tenant.footer_bank)    if is_en else tenant.footer_bank
        _director= (tenant.footer_director_en or tenant.footer_director) if is_en else tenant.footer_director
        if _name:    lines.append(f'<b>{pdf_text(_name)}</b>')
        if tenant.footer_vat: lines.append(f'ID: {pdf_text(tenant.footer_vat)}' if is_en else (f'P.IVA: {pdf_text(tenant.footer_vat)}' if is_it else f'ს/კ {pdf_text(tenant.footer_vat)}'))
        if _address: lines.append(pdf_text(_address))
        if tenant.footer_phone: lines.append(f'Tel: {pdf_text(tenant.footer_phone)}' if is_en else (f'Tel: {pdf_text(tenant.footer_phone)}' if is_it else f'ტელ: {pdf_text(tenant.footer_phone)}'))
        if tenant.footer_email: lines.append(f'Email: {pdf_text(tenant.footer_email)}' if is_en else (f'Email: {pdf_text(tenant.footer_email)}' if is_it else f'მეილი: {pdf_text(tenant.footer_email)}'))
        if _bank:    lines.append(f'Bank: {pdf_text(_bank)}' if is_en else (f'Banca: {pdf_text(_bank)}' if is_it else f'ბანკი: {pdf_text(_bank)}'))
        if tenant.footer_bank_code: lines.append(f'Bank code: {pdf_text(tenant.footer_bank_code)}')
        if tenant.footer_iban: lines.append(f'A/A: {pdf_text(tenant.footer_iban)}')
        executor = '<br/>'.join(lines)
        director = _director or ''
    else:
        executor = ''
        director = ''

    # Signature from Supabase
    sig_img = p('')
    if tenant and tenant.signature_path:
        sig_bytes = get_signature_bytes(tenant.signature_path, tenant.id)
        if sig_bytes:
            sig_buf = io.BytesIO(sig_bytes)
            sig_img = Image(sig_buf, width=6*cm, height=6*cm)

    rc = Table([
        [p(f'Director<br/>{pdf_text(director)}' if is_en else (f'Direttore<br/>{pdf_text(director)}' if is_it else f'დირექტორი<br/>{pdf_text(director)}'), size=10, leading=15, align='CENTER', markup=True)],
        [sig_img],
    ], colWidths=[uw*0.38])
    rc.setStyle(TableStyle([
        ('ALIGN',(0,0),(-1,-1),'CENTER'),('VALIGN',(0,0),(-1,-1),'TOP'),
        ('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),0),
        ('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),
    ]))
    t7 = Table([[p(executor, size=10, leading=15, markup=True), rc]], colWidths=[uw*0.62, uw*0.38])
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
    raw   = (d.get('email') or '').strip().lower()
    pwd   = d.get('password') or ''
    if not auth_attempt_allowed(raw):
        return jsonify({'error': 'Too many attempts. Try again in 15 minutes.'}), 429
    user  = User.query.filter_by(email=raw).first()
    if not user and '@' not in raw:
        # Try matching username portion of any email
        user = User.query.filter(User.email.startswith(raw + '@', autoescape=True)).first()
    if not user or not check_password(pwd, user.password_hash):
        record_auth_failure(raw)
        return jsonify({'error': 'არასწორი მეილი ან პაროლი'}), 401
    if LEGACY_HASH.fullmatch(user.password_hash):
        # Upgrade legacy hashes without forcing existing users to reset immediately.
        from werkzeug.security import generate_password_hash
        user.password_hash = generate_password_hash(pwd, method='scrypt')
        db.session.commit()
    session.clear()
    session.permanent = True
    session['auth_stamp'] = session_stamp(user.password_hash, app.secret_key)
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
    pwd   = d.get('password') or ''
    name  = (d.get('name') or '').strip()
    if not email or not pwd or not name:
        return jsonify({'error': 'სახელი, მეილი და პაროლი სავალდებულოა'}), 400
    validate_email(email)
    if not password_is_valid(pwd):
        abort(400, 'Passwords must contain 12 to 1024 characters.')
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'ეს მეილი უკვე გამოყენებულია'}), 400
    require_text(d, 'name')
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
    if tid == current_user().tenant_id:
        abort(400, 'You cannot delete your own administrator tenant.')
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
    if 'email_from'       in d: t.email_from       = d['email_from'].strip() or None
    if 'email_subject'    in d: t.email_subject    = d['email_subject'].strip() or None
    if 'email_body'       in d: t.email_body       = d['email_body'].strip() or None
    if 'footer_name_en'   in d: t.footer_name_en   = d['footer_name_en'].strip() or None
    if 'footer_address_en' in d: t.footer_address_en = d['footer_address_en'].strip() or None
    if 'footer_director_en' in d: t.footer_director_en = d['footer_director_en'].strip() or None
    if 'footer_bank_en'   in d: t.footer_bank_en   = d['footer_bank_en'].strip() or None
    if 'email_subject_en' in d: t.email_subject_en = d['email_subject_en'].strip() or None
    if 'email_body_en'    in d: t.email_body_en    = d['email_body_en'].strip() or None
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/settings/signature', methods=['POST'])
@login_required
def upload_signature():
    u = current_user()
    t = db.session.get(Tenant, u.tenant_id)
    if 'file' not in request.files:
        return jsonify({'error': 'ფაილი არ არის'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'ფაილი არ არის'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in ('png', 'jpg', 'jpeg'):
        return jsonify({'error': 'მხოლოდ PNG ან JPG'}), 400
    file_bytes = f.read(2 * 1024 * 1024 + 1)
    if len(file_bytes) > 2 * 1024 * 1024:
        return jsonify({'error': 'ფაილი 2MB-ზე მეტია'}), 400
    try:
        file_bytes = sanitize_signature(file_bytes)
        path = upload_signature_to_supabase(file_bytes, 'signature.png', u.tenant_id)
        t.signature_path = path
        db.session.commit()
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        app.logger.error('Signature upload failed.')
        return jsonify({'error': 'Signature upload failed. Check storage configuration.'}), 502

@app.route('/api/settings/signature', methods=['GET'])
@login_required
def get_signature():
    u = current_user()
    t = db.session.get(Tenant, u.tenant_id)
    if not t or not t.signature_path:
        return jsonify({'error': 'არ არის'}), 404
    sig_bytes = get_signature_bytes(t.signature_path, t.id)
    if not sig_bytes:
        return jsonify({'error': 'ვერ ჩაიტვირთა'}), 404
    ext = t.signature_path.rsplit('.', 1)[-1].lower()
    mime = 'image/png' if ext == 'png' else 'image/jpeg'
    from flask import Response
    return Response(sig_bytes, mimetype=mime)


@app.route('/api/settings/smtp', methods=['PUT'])
@login_required
def save_smtp():
    u = current_user()
    t = db.session.get(Tenant, u.tenant_id)
    d = request.get_json()
    t.smtp_email    = (d.get('smtp_email') or '').strip() or None
    t.smtp_host     = (d.get('smtp_host') or '').strip() or None
    t.smtp_port     = int(d.get('smtp_port') or 465)
    validate_email(t.smtp_email)
    smtp_destination(t.smtp_host, t.smtp_port)
    # Only update password if a new one was provided
    new_pwd = d.get('smtp_password') or ''
    if new_pwd:
        t.smtp_password = encrypt_credential(new_pwd)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/settings/smtp/test', methods=['POST'])
@login_required
def test_smtp():
    u = current_user()
    t = db.session.get(Tenant, u.tenant_id)
    if t.smtp_email and t.smtp_password and t.smtp_host:
        try:
            with open_smtp(t.smtp_host, int(t.smtp_port or 465)) as srv:
                srv.login(t.smtp_email, decrypt_credential(t.smtp_password))
            return jsonify({'ok': True})
        except Exception:
            return jsonify({'error': 'SMTP connection failed. Check host, TLS and credentials.'}), 400
    else:
        try:
            import win32com.client
            outlook = win32com.client.Dispatch('Outlook.Application')
            outlook.GetNamespace('MAPI')
            return jsonify({'ok': True})
        except Exception:
            return jsonify({'error': 'Outlook is unavailable on this computer.'}), 400

@app.route('/api/invoices/<int:inv_id>/send', methods=['POST'])
@login_required
def send_invoice(inv_id):
    u = current_user()
    t = db.session.get(Tenant, u.tenant_id)
    inv = Invoice.query.filter_by(id=inv_id, tenant_id=u.tenant_id).with_for_update().first_or_404()
    if inv.sent:
        abort(409, 'This invoice has already been sent.')

    try:
        pdf_data = build_pdf(inv)

        # Build subject and body from template (fallback to defaults)
        inv_lang = inv.company.language or 'ka'
        if inv_lang == 'en':
            default_subject = f'Invoice #{inv.number}'
            default_body    = (f'Dear Sir/Madam,\n\nPlease find attached invoice '
                               f'#{inv.number}.\n\nKind regards,\n{t.name}')
            subject = (t.email_subject_en or default_subject).replace('{number}', str(inv.number))
            body    = (t.email_body_en    or default_body   ).replace('{number}', str(inv.number))
        elif inv_lang == 'it':
            default_subject = f'Fattura #{inv.number}'
            default_body    = (f'Gentile Cliente,\n\nIn allegato trova la fattura '
                               f'#{inv.number}.\n\nCordiali saluti,\n{t.name}')
            subject = (t.email_subject_en or default_subject).replace('{number}', str(inv.number))
            body    = (t.email_body_en    or default_body   ).replace('{number}', str(inv.number))
        else:
            default_subject = f'ინვოისი #{inv.number}'
            default_body    = (f'გამარჯობა,\n\nგთხოვთ იხილოთ თანდართული ინვოისი '
                               f'#{inv.number}.\n\nპატივისცემით,\n{t.name}')
            subject = (t.email_subject or default_subject).replace('{number}', str(inv.number))
            body    = (t.email_body    or default_body   ).replace('{number}', str(inv.number))

        if t.smtp_email and t.smtp_password and t.smtp_host:
            # ── SMTP path (cloud / any platform) ──────────────────────────
            import smtplib
            from email.message import EmailMessage
            msg = EmailMessage()
            msg['Subject'] = subject
            msg['From']    = t.smtp_email
            recipients = [e.strip() for e in inv.company.email.split(',') if e.strip()]
            msg['To']      = ', '.join(recipients)
            msg.set_content(body)
            msg.add_attachment(pdf_data, maintype='application', subtype='pdf',
                               filename=f'invoice_{inv.number}.pdf')
            with open_smtp(t.smtp_host, int(t.smtp_port or 465)) as srv:
                srv.login(t.smtp_email, decrypt_credential(t.smtp_password))
                srv.send_message(msg, to_addrs=recipients)
        else:
            # ── Outlook path (local Windows only) ─────────────────────────
            import win32com.client, tempfile, os, pythoncom
            pythoncom.CoInitialize()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf',
                                              prefix=f'invoice_{inv.number}_')
            tmp.write(pdf_data)
            tmp.close()
            outlook = win32com.client.Dispatch('Outlook.Application')
            # Switch to the right sender account if email_from is set
            mail = outlook.CreateItem(0)
            if t.email_from:
                try:
                    for account in outlook.Session.Accounts:
                        if account.SmtpAddress.lower() == t.email_from.lower():
                            mail._oleobj_.Invoke(*(64209, 0, 8, 0, account))
                            break
                except Exception:
                    pass
            mail.To      = '; '.join([e.strip() for e in inv.company.email.split(',') if e.strip()])
            mail.Subject = subject
            mail.Body    = body
            mail.Attachments.Add(tmp.name)
            mail.Send()
            os.unlink(tmp.name)
            pythoncom.CoUninitialize()

        inv.sent = True
        inv.send_error = None
        db.session.commit()
        return jsonify({'ok': True})
    except Exception:
        inv.send_error = 'Delivery failed. Check mail configuration before retrying.'
        db.session.commit()
        return jsonify({'error': inv.send_error}), 400
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
    validate_company(d)
    co = Company(tenant_id=u.tenant_id,
                 name=d['name'].strip(), vat=d['vat'].strip(),
                 address=d['address'].strip(), email=d['email'].strip(),
                 legal_form=d.get('legal_form','შპს'),
                 language=d.get('language','ka'))
    db.session.add(co); db.session.commit()
    return jsonify(co.to_dict()), 201

@app.route('/api/companies/<int:cid>', methods=['PUT'])
@login_required
def update_company(cid):
    u  = current_user()
    co = Company.query.filter_by(id=cid, tenant_id=u.tenant_id).first_or_404()
    d  = request.get_json()
    validate_company(d)
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
    co.language   = d.get('language', 'ka')
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
    validate_product(d)
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
    validate_product(d)
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
        items, amount_str = validate_invoice_items(items)
        if custom_date:
            try:
                datetime.strptime(custom_date, '%d/%m/%Y')
            except ValueError:
                abort(400, 'Invoice date must use DD/MM/YYYY.')
        show_details = d.get('show_details', True)
        if not isinstance(show_details, bool):
            abort(400, 'show_details must be a boolean.')
        # Serialize number allocation per tenant on PostgreSQL.
        db.session.execute(db.select(Tenant).where(Tenant.id == u.tenant_id).with_for_update()).scalar_one()
        inv = Invoice(
            tenant_id   = u.tenant_id,
            number      = next_invoice_number(u.tenant_id),
            company_id  = company.id,
            amount      = amount_str,
            items       = json.dumps(items, ensure_ascii=False),
            custom_date = custom_date if custom_date else None,
        )
        db.session.add(inv); db.session.flush()
        pdf_b64 = base64.b64encode(build_pdf(inv, show_details=show_details)).decode()
        db.session.commit()
        return jsonify({'invoice_id': inv.id, 'invoice_number': inv.number, 'pdf': pdf_b64})
    except HTTPException:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        app.logger.error('Invoice generation failed.')
        return jsonify({'error': 'Invoice generation failed.'}), 500

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
    if not auth_attempt_allowed(u.email):
        return jsonify({'error': 'Too many attempts. Try again in 15 minutes.'}), 429
    if not check_password(cur, u.password_hash):
        record_auth_failure(u.email)
        return jsonify({'error': 'მიმდინარე პაროლი არასწორია'}), 400
    if not password_is_valid(new):
        return jsonify({'error': 'Passwords must contain 12 to 1024 characters.'}), 400
    u.password_hash = hash_password(new)
    db.session.commit()
    if u.id == session.get('user_id'):
        session['auth_stamp'] = session_stamp(u.password_hash, app.secret_key)
    return jsonify({'ok': True})

@app.route('/api/auth/change-email', methods=['POST'])
@login_required
def change_email():
    u     = current_user()
    d     = request.get_json()
    cur   = d.get('current_password', '')
    email = (d.get('new_email') or '').strip().lower()
    if not auth_attempt_allowed(u.email):
        return jsonify({'error': 'Too many attempts. Try again in 15 minutes.'}), 429
    if not check_password(cur, u.password_hash):
        record_auth_failure(u.email)
        return jsonify({'error': 'მიმდინარე პაროლი არასწორია'}), 400
    validate_email(email)
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
    if not password_is_valid(new):
        return jsonify({'error': 'Passwords must contain 12 to 1024 characters.'}), 400
    u = User.query.filter_by(tenant_id=tid).first()
    if not u:
        return jsonify({'error': 'User not found'}), 404
    u.password_hash = hash_password(new)
    db.session.commit()
    if u.id == session.get('user_id'):
        session['auth_stamp'] = session_stamp(u.password_hash, app.secret_key)
    return jsonify({'ok': True})

@app.route('/api/admin/tenants/<int:tid>/reset-email', methods=['POST'])
@login_required
def admin_reset_email(tid):
    if current_user().role != 'superadmin':
        return jsonify({'error': 'Forbidden'}), 403
    d     = request.get_json()
    email = (d.get('new_email') or '').strip().lower()
    validate_email(email)
    if User.query.filter(User.email == email, User.tenant_id != tid).first():
        return jsonify({'error': 'ეს მეილი უკვე გამოყენებულია'}), 400
    u = User.query.filter_by(tenant_id=tid).first()
    if not u:
        return jsonify({'error': 'User not found'}), 404
    u.email = email
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/invoices', methods=['GET'])
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
        ('tenant', 'email_from',    'VARCHAR(255)'),
        ('tenant', 'email_subject', 'VARCHAR(500)'),
        ('tenant', 'email_body',    'TEXT'),
        ('invoice', 'send_error',  'VARCHAR(255)'),
        ('invoice', 'custom_date', 'VARCHAR(20)'),
        ('invoice', 'items',       'TEXT'),
        ('invoice', 'tenant_id',   'INTEGER'),
        ('company', 'legal_form',  "VARCHAR(20) DEFAULT 'შპს'"),
        ('company', 'status',      "VARCHAR(20) DEFAULT 'active'"),
        ('company', 'tenant_id',   'INTEGER'),
        ('product', 'vat',         "VARCHAR(5) DEFAULT 'no'"),
        ('product', 'tenant_id',   'INTEGER'),
        ('tenant', 'footer_name_en',    'VARCHAR(255)'),
        ('tenant', 'footer_address_en', 'VARCHAR(500)'),
        ('tenant', 'footer_director_en','VARCHAR(255)'),
        ('tenant', 'footer_bank_en',    'VARCHAR(255)'),
        ('tenant', 'email_subject_en',  'VARCHAR(500)'),
        ('tenant', 'email_body_en',     'TEXT'),
        ('company', 'language',         "VARCHAR(2) DEFAULT 'ka'"),
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

    if db.engine.dialect.name == 'postgresql':
        # Older desktop builds used VARCHAR(255); encrypted tokens need more room.
        with db.engine.begin() as conn:
            conn.execute(sqlalchemy.text(
                'ALTER TABLE tenant ALTER COLUMN smtp_password TYPE VARCHAR(500)'
            ))

def ensure_superadmin():
    """Bootstrap only from explicitly supplied credentials; never print passwords."""
    existing = User.query.filter_by(role='superadmin').first()
    if existing and not check_password('changeme123', existing.password_hash):
        return
    email = os.environ.get('SUPERADMIN_EMAIL', '').strip().lower()
    password = os.environ.get('SUPERADMIN_PASSWORD', '')
    if not email or not password_is_valid(password):
        raise RuntimeError('Set SUPERADMIN_EMAIL and a SUPERADMIN_PASSWORD of at least 12 characters to bootstrap or replace the old default administrator.')
    validate_email(email, use_abort=False)
    if existing:
        existing.email = email
        existing.password_hash = hash_password(password)
    else:
        tenant = Tenant(name='Super Admin', invoice_prefix=90000)
        db.session.add(tenant)
        db.session.flush()
        db.session.add(User(tenant_id=tenant.id, email=email,
                            password_hash=hash_password(password), role='superadmin'))
    db.session.commit()
    app.logger.info('Administrator initialised from environment configuration.')


@app.cli.command('encrypt-smtp-passwords')
def encrypt_smtp_passwords():
    """Encrypt legacy plaintext SMTP credentials using the external Fernet key."""
    count = 0
    for tenant in Tenant.query.filter(Tenant.smtp_password.isnot(None)).all():
        if tenant.smtp_password and not tenant.smtp_password.startswith('fernet:'):
            tenant.smtp_password = encrypt_credential(tenant.smtp_password)
            count += 1
    db.session.commit()
    click.echo(f'Encrypted SMTP credentials for {count} tenant(s).')


with app.app_context():
    db.create_all()
    run_migrations()
    ensure_superadmin()

if __name__ == '__main__':
    app.run(host='127.0.0.1', debug=False)


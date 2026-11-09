import base64
import hashlib
import io
import json
import re
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from PIL import Image
from werkzeug.security import generate_password_hash

import app as module
import security
from conftest import csrf, login


def invoice_payload(company_id=1, **changes):
    return {'company_id': company_id, 'date': '09/11/2026',
            'items': [{'name': 'Storage', 'unit': 'day', 'qty': '2',
                       'price': '10.00', 'total': '0.01'}], **changes}


def test_csrf_required_including_login_and_upload(client):
    assert client.post('/api/auth/login', json={'email': 'x', 'password': 'x'}).status_code == 403
    headers = login(client)
    assert client.delete('/api/companies/1').status_code == 403
    assert client.post('/api/settings/signature', data={}).status_code == 403
    assert client.post('/api/auth/logout', headers=headers).status_code == 200


def test_csrf_rejects_token_from_different_session(client):
    other = module.app.test_client()
    assert client.post('/api/auth/login', headers=csrf(other), json={}).status_code == 403


def test_security_headers_and_cookie_flags(client):
    response = client.get('/login')
    cookie = response.headers['Set-Cookie']
    assert all(flag in cookie for flag in ('Secure', 'HttpOnly', 'SameSite=Lax'))
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Cache-Control'] == 'no-store'
    assert "object-src 'none'" in response.headers['Content-Security-Policy']
    assert client.get('/login', headers={'Host': 'attacker.invalid'}).status_code == 400


def test_unauthenticated_api_returns_json(client):
    response = client.get('/api/companies')
    assert response.status_code == 401
    assert response.is_json


def test_legacy_hash_is_upgraded_after_login(client):
    salt = 'a' * 32
    legacy = salt + ':' + hashlib.sha256((salt + 'old-secret').encode()).hexdigest()
    with module.app.app_context():
        module.User.query.filter_by(email='first@example.invalid').one().password_hash = legacy
        module.db.session.commit()
    login(client, password='old-secret')
    with module.app.app_context():
        stored = module.User.query.filter_by(email='first@example.invalid').one().password_hash
        assert stored.startswith('scrypt:')
        assert security.check_password('old-secret', stored)


def test_password_hashing_and_policy():
    stored = security.hash_password('a-long-enough-password')
    assert stored.startswith('scrypt:')
    assert security.check_password('a-long-enough-password', stored)
    assert not security.check_password('wrong-password', stored)
    assert not security.check_password('x' * 1025, stored)
    with pytest.raises(ValueError):
        security.hash_password('short')


def test_login_username_wildcards_do_not_match_arbitrary_accounts(client):
    response = client.post('/api/auth/login', headers=csrf(client),
                           json={'email': '%', 'password': 'test-user-password'})
    assert response.status_code == 401


def test_login_throttling_is_persistent_and_expiring(client):
    headers = csrf(client)
    for _ in range(10):
        assert client.post('/api/auth/login', headers=headers,
                           json={'email': 'first@example.invalid', 'password': 'bad'}).status_code == 401
    assert client.post('/api/auth/login', headers=headers,
                       json={'email': 'first@example.invalid', 'password': 'test-user-password'}).status_code == 429
    with module.app.app_context():
        module.AuthFailure.query.update({'created_at': datetime.utcnow() - timedelta(minutes=16)})
        module.db.session.commit()
    login(client)


def test_password_change_invalidates_other_sessions(client):
    headers = login(client)
    other = module.app.test_client()
    login(other)
    response = client.post('/api/auth/change-password', headers=headers,
                           json={'current_password': 'test-user-password', 'new_password': 'another-long-password'})
    assert response.status_code == 200
    assert other.get('/api/auth/me').status_code == 401
    assert client.get('/api/auth/me').status_code == 200


def test_admin_reset_invalidates_user_sessions(client):
    login(client)
    admin = module.app.test_client()
    headers = login(admin, 'admin@example.invalid', 'test-only-admin-password')
    with module.app.app_context():
        tid = module.User.query.filter_by(email='first@example.invalid').one().tenant_id
    assert admin.post(f'/api/admin/tenants/{tid}/reset-password', headers=headers,
                      json={'new_password': 'changed-by-admin-password'}).status_code == 200
    assert client.get('/api/auth/me').status_code == 401


def test_weak_new_password_is_rejected_in_all_routes(client):
    headers = login(client)
    assert client.post('/api/auth/change-password', headers=headers,
                       json={'current_password': 'test-user-password', 'new_password': 'short'}).status_code == 400
    admin = module.app.test_client()
    headers = login(admin, 'admin@example.invalid', 'test-only-admin-password')
    assert admin.post('/api/admin/tenants', headers=headers,
                      json={'email': 'new@example.invalid', 'name': 'New', 'password': 'short'}).status_code == 400
    assert admin.post('/api/admin/tenants/2/reset-password', headers=headers,
                      json={'new_password': 'short'}).status_code == 400


def test_no_implicit_default_administrator(monkeypatch, capsys):
    with module.app.app_context():
        module.User.query.filter_by(role='superadmin').delete()
        module.db.session.commit()
        monkeypatch.delenv('SUPERADMIN_PASSWORD')
        with pytest.raises(RuntimeError, match='SUPERADMIN_PASSWORD'):
            module.ensure_superadmin()
    assert 'test-only-admin-password' not in capsys.readouterr().out


def test_old_default_administrator_requires_explicit_replacement(monkeypatch):
    with module.app.app_context():
        user = module.User.query.filter_by(role='superadmin').one()
        user.password_hash = generate_password_hash('changeme123', method='scrypt')
        module.db.session.commit()
        monkeypatch.delenv('SUPERADMIN_PASSWORD')
        with pytest.raises(RuntimeError):
            module.ensure_superadmin()


def test_tenant_isolation_for_read_write_and_generate(client):
    headers = login(client)
    companies = client.get('/api/companies').get_json()
    products = client.get('/api/products').get_json()
    assert [item['name'] for item in companies] == ['first client']
    assert [item['name'] for item in products] == ['first service']
    with module.app.app_context():
        foreign = module.Company.query.filter_by(name='second client').one().id
        foreign_product = module.Product.query.filter_by(name='second service').one().id
    for method in ('put', 'delete'):
        assert getattr(client, method)(f'/api/companies/{foreign}', headers=headers, json={}).status_code == 404
        assert getattr(client, method)(f'/api/products/{foreign_product}', headers=headers, json={}).status_code == 404
    assert client.post('/api/invoices/generate', headers=headers,
                       json=invoice_payload(foreign)).status_code == 404
    assert client.get('/api/admin/tenants').status_code == 403
    assert client.get('/api/admin/invoices').status_code == 403


def test_invoice_is_calculated_on_server_and_pdf_accepts_literal_markup(client):
    headers = login(client)
    company = client.get('/api/companies').get_json()[0]
    company.update(name='<img src="http://127.0.0.1/secret"> & Client',
                   address='<b>Literal address</b>')
    company_id = company.pop("id")
    assert client.put(f'/api/companies/{company_id}', headers=headers, json=company).status_code == 200
    assert client.put('/api/settings', headers=headers,
                      json={'footer_name': '<img src="http://127.0.0.1/secret">',
                            'footer_director': '<b>Literal director</b>'}).status_code == 200
    payload = invoice_payload(company_id)
    payload['items'][0]['name'] = '<img src="file:///etc/passwd"> & service'
    response = client.post('/api/invoices/generate', headers=headers, json=payload)
    assert response.status_code == 200, response.get_json()
    assert base64.b64decode(response.get_json()['pdf']).startswith(b'%PDF-')
    invoice = client.get('/api/invoices').get_json()[0]
    assert invoice['amount'] == '20.00'
    assert invoice['items'][0]['total'] == '20.00'


@pytest.mark.parametrize('items', [[], [{}], 'bad', [None], [{'name': 'x', 'price': 'NaN'}],
                                 [{'name': 'x', 'price': '-1'}], [{'name': 'x', 'qty': 'Infinity'}]])
def test_malformed_invoice_items_are_rejected(client, items):
    headers = login(client)
    response = client.post('/api/invoices/generate', headers=headers,
                           json=invoice_payload(items=items))
    assert response.status_code == 400


@pytest.mark.parametrize('body', [[], None, {'name': {'nested': 'value'}}, {'email': '\r\nBcc: x'}])
def test_invalid_json_shapes_do_not_raise_internal_errors(client, body):
    headers = login(client)
    response = client.post('/api/companies', headers=headers, json=body)
    assert response.status_code in (400, 415)


def test_invoice_creation_rolls_back_on_pdf_failure(client, monkeypatch):
    headers = login(client)
    monkeypatch.setattr(module, 'build_pdf', Mock(side_effect=RuntimeError('private-db-password')))
    response = client.post('/api/invoices/generate', headers=headers, json=invoice_payload())
    assert response.status_code == 500
    assert b'private-db-password' not in response.data
    assert client.get('/api/invoices').get_json() == []


def test_signature_rejects_fake_images_and_oversized_requests(client):
    headers = login(client)
    response = client.post('/api/settings/signature', headers=headers,
                           data={'file': (io.BytesIO(b'<script>alert(1)</script>'), 'fake.png')})
    assert response.status_code == 400
    response = client.post('/api/settings/signature', headers=headers,
                           data={'file': (io.BytesIO(b'x' * (3 * 1024 * 1024)), 'large.png')})
    assert response.status_code == 413


def test_signature_reencodes_image_and_is_tenant_scoped(client, monkeypatch):
    headers = login(client)
    buf = io.BytesIO()
    Image.new('RGB', (10, 10), 'white').save(buf, format='JPEG')
    upload = Mock(return_value='tenant_2/signature.png')
    monkeypatch.setattr(module, 'upload_signature_to_supabase', upload)
    response = client.post('/api/settings/signature', headers=headers,
                           data={'file': (io.BytesIO(buf.getvalue()), 'signature.jpg')})
    assert response.status_code == 200
    content, filename, tid = upload.call_args.args
    assert content.startswith(b'\x89PNG')
    assert filename == 'signature.png' and tid == 2
    assert module.get_signature_bytes('tenant_3/signature.png', 2) is None


def test_smtp_secrets_encrypted_and_not_returned(client, monkeypatch):
    headers = login(client)
    monkeypatch.setattr(module, 'smtp_destination', lambda *args: ('smtp.example.invalid', ['8.8.8.8']))
    response = client.put('/api/settings/smtp', headers=headers,
                          json={'smtp_email': 'sender@example.invalid', 'smtp_host': 'smtp.example.invalid',
                                'smtp_port': '465', 'smtp_password': 'secret-mail-password'})
    assert response.status_code == 200
    with module.app.app_context():
        encrypted = module.Tenant.query.filter_by(name='first').one().smtp_password
        assert encrypted.startswith('fernet:') and 'secret-mail-password' not in encrypted
        assert security.decrypt_credential(encrypted) == 'secret-mail-password'
    assert b'secret-mail-password' not in client.get('/api/settings').data


def test_legacy_smtp_migration_is_idempotent():
    with module.app.app_context():
        module.Tenant.query.filter_by(name='first').one().smtp_password = 'legacy-mail-secret'
        module.db.session.commit()
    runner = module.app.test_cli_runner()
    first = runner.invoke(args=['encrypt-smtp-passwords'])
    second = runner.invoke(args=['encrypt-smtp-passwords'])
    assert first.exit_code == second.exit_code == 0
    assert '1 tenant' in first.output and '0 tenant' in second.output
    assert 'legacy-mail-secret' not in first.output


def test_smtp_host_allowlist_and_private_address_protection(monkeypatch):
    monkeypatch.setenv('SMTP_ALLOWED_HOSTS', 'smtp.example.invalid')
    monkeypatch.setattr(security.socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 465))])
    with pytest.raises(ValueError, match='public'):
        security.smtp_destination('smtp.example.invalid', 465)
    with pytest.raises(ValueError, match='SMTP_ALLOWED_HOSTS'):
        security.smtp_destination('other.example.invalid', 465)
    with pytest.raises(ValueError, match='465 or 587'):
        security.smtp_destination('smtp.example.invalid', 22)


def test_smtp_accepts_only_validated_public_destinations(monkeypatch):
    monkeypatch.setenv('SMTP_ALLOWED_HOSTS', 'smtp.example.invalid')
    monkeypatch.setattr(security.socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('8.8.8.8', 465))])
    assert security.smtp_destination('smtp.example.invalid', 465) == ('smtp.example.invalid', ['8.8.8.8'])


def test_frontend_does_not_embed_record_text_in_event_handlers(client):
    login(client)
    html = client.get('/').data.decode()
    assert "${escHtml(c.name)}')" not in html
    assert "${escHtml(pr.name)}')" not in html
    assert "${escHtml(tn.email)}')" not in html
    assert '${escHtml(msg)}' in html
    assert '<td>${escHtml(inv.date)}</td>' in html
    assert 'apiFetch(' in html


def test_foreign_invoice_cannot_be_sent_or_deleted(client):
    headers = login(client)
    created = client.post('/api/invoices/generate', headers=headers, json=invoice_payload()).get_json()
    other = module.app.test_client()
    other_headers = login(other, 'second@example.invalid')
    invoice_id = created['invoice_id']
    assert other.post(f'/api/invoices/{invoice_id}/send', headers=other_headers).status_code == 404
    assert other.delete(f'/api/invoices/{invoice_id}', headers=other_headers).status_code == 404
    assert other.get('/api/invoices').get_json() == []


def test_sent_invoice_cannot_be_sent_again(client):
    headers = login(client)
    created = client.post('/api/invoices/generate', headers=headers, json=invoice_payload()).get_json()
    with module.app.app_context():
        module.db.session.get(module.Invoice, created['invoice_id']).sent = True
        module.db.session.commit()
    response = client.post(f'/api/invoices/{created["invoice_id"]}/send', headers=headers)
    assert response.status_code == 409


@pytest.mark.parametrize('port', [465, 587])
def test_smtp_pins_ip_and_verifies_hostname_and_certificate(monkeypatch, port):
    import ssl
    monkeypatch.setattr(security, 'smtp_destination', lambda *a: ('smtp.example.invalid', ['8.8.8.8']))
    sock = Mock()
    connect = Mock(return_value=sock)
    monkeypatch.setattr(security.socket, 'create_connection', connect)
    monkeypatch.setattr(security.smtplib.SMTP, 'getreply', lambda self: (220, b'ready'))
    def ehlo(self):
        self.ehlo_resp = b'ready'
        self.does_esmtp = True
        self.esmtp_features = {'starttls': ''}
        return 250, b'ready'
    monkeypatch.setattr(security.smtplib.SMTP, 'ehlo', ehlo)
    monkeypatch.setattr(security.smtplib.SMTP, 'docmd', lambda *a: (220, b'ready'))
    captures = []
    def wrap(context, socket, **kwargs):
        captures.append((context, kwargs))
        return socket
    monkeypatch.setattr(security.ssl.SSLContext, 'wrap_socket', wrap)
    client = security.open_smtp('smtp.example.invalid', port)
    assert connect.call_args.args[0] == ('8.8.8.8', port)
    context, kwargs = captures[0]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert kwargs['server_hostname'] == 'smtp.example.invalid'
    client.close()


def test_admin_cannot_delete_own_tenant(client):
    headers = login(client, 'admin@example.invalid', 'test-only-admin-password')
    tid = client.get('/api/auth/me').get_json()['tenant_id']
    assert client.delete(f'/api/admin/tenants/{tid}', headers=headers).status_code == 400

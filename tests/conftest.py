import os
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

# Tests never connect to a production database, storage service or mail server.
os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['SECRET_KEY'] = 'test-only-key-' + 'a' * 48
os.environ['SUPERADMIN_EMAIL'] = 'admin@example.invalid'
os.environ['SUPERADMIN_PASSWORD'] = 'test-only-admin-password'
os.environ['CREDENTIAL_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
os.environ['TRUSTED_HOSTS'] = 'localhost,127.0.0.1'
os.environ.pop('LOCAL_HTTP', None)
if not (Path(__file__).resolve().parents[1] / 'fonts/DejaVuSans.ttf').exists():
    os.environ['FONT_DIR'] = '/usr/share/fonts/truetype/dejavu'

import app as module


@pytest.fixture(autouse=True)
def database():
    module.app.config.update(TESTING=True)
    with module.app.app_context():
        module.db.drop_all()
        module.db.create_all()
        module.ensure_superadmin()
        for label in ('first', 'second'):
            tenant = module.Tenant(name=label, footer_name='Example & Co')
            module.db.session.add(tenant)
            module.db.session.flush()
            module.db.session.add(module.User(
                tenant_id=tenant.id, email=f'{label}@example.invalid',
                password_hash=module.hash_password('test-user-password')))
            module.db.session.add(module.Company(
                tenant_id=tenant.id, name=f'{label} client', vat='123456789',
                address='Example street', email='customer@example.invalid'))
            module.db.session.add(module.Product(
                tenant_id=tenant.id, name=f'{label} service', unit='unit', price='10'))
        module.db.session.commit()
    yield
    with module.app.app_context():
        module.db.session.remove()


@pytest.fixture
def client():
    return module.app.test_client()


def csrf(client):
    response = client.get('/login', follow_redirects=True)
    token = re.search(rb'name="csrf-token" content="([a-f0-9]+)"', response.data)
    assert token, response.status_code
    return {'X-CSRF-Token': token.group(1).decode()}


def login(client, email='first@example.invalid', password='test-user-password'):
    response = client.post('/api/auth/login', headers=csrf(client),
                           json={'email': email, 'password': password})
    assert response.status_code == 200, response.get_json()
    return csrf(client)

# Deployment and upgrades

The desktop launcher is intended for a trusted workstation. For shared access, deploy the app centrally behind HTTPS so users do not need direct database credentials on their computers.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLite for a local demo, PostgreSQL with TLS for a shared deployment |
| `SECRET_KEY` | Persistent random session key, at least 32 characters, identical across web workers |
| `SUPERADMIN_EMAIL` / `SUPERADMIN_PASSWORD` | Explicit initial administrator credentials; password minimum 12 characters |
| `LOCAL_HTTP` | `true` only for a loopback demonstration; `false` for HTTPS hosting |
| `TRUSTED_HOSTS` | Comma-separated application hostnames; no URL scheme or port |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet key for SMTP credentials; protect and back up separately from the database |
| `SMTP_ALLOWED_HOSTS` | Exact SMTP provider hostnames approved by the operator |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Optional server-side configuration for the private `signatures` bucket |

SMTP uses certificate-verified TLS on port 465 or 587, and the configured host must resolve to public IP addresses. Private mail relays require a separately reviewed deployment policy. Keep storage service keys on the server and ensure the bucket is private.

## Upgrade an existing installation

1. Back up the database and current environment configuration, and avoid upgrading during invoice sends.
2. Install `requirements.txt` and configure the variables above. Keep existing non-default administrator credentials and stable session/encryption keys. Startup requires explicit administrator configuration when bootstrapping an installation.
3. Run `flask --app app encrypt-smtp-passwords` before sending mail on an installation with existing SMTP settings. It converts any unencrypted values and can be rerun safely. PostgreSQL startup also ensures the SMTP-password column has room for encrypted values.
4. Expect users to sign in again after upgrading. Test login, tenant isolation, a PDF and delivery to your own test mailbox before normal use.
5. Rebuild any Windows executable from the reviewed source. Supply `.env` separately on each trusted installation; the build script does not copy it.

For a web deployment, launch Gunicorn behind an HTTPS reverse proxy. Example for a proxy running on the same server:

```bash
gunicorn --bind 127.0.0.1:8000 --workers 2 app:app
```

Add reverse-proxy login rate limits appropriate to your deployment. Application account/IP failure counters are database-backed and best-effort under concurrent requests. The existing interface uses inline JavaScript handlers, so its Content Security Policy still permits inline scripts.

## Verification scope

Local regression tests use SQLite and mocked external services. Verify PostgreSQL migrations and locking, Windows packaging/Outlook, SMTP delivery and Supabase connectivity in staging with your own test accounts. A browser check and a dependency scan complement the tests; they do not validate the configuration of a live deployment.

The included screenshot uses fictional customer records and `.invalid` email addresses. Do not commit actual customer data, generated invoices, signatures, environment files or configured executables.

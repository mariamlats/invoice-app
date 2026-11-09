"""Security primitives shared by the web app and desktop launcher."""

import hashlib
import hmac
import ipaddress
import os
import re
import smtplib
import socket
import ssl

from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash, generate_password_hash


MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
LEGACY_HASH = re.compile(r"[0-9a-f]{32}:[0-9a-f]{64}\Z")
ENCRYPTED_PREFIX = "fernet:"


def password_is_valid(password):
    return (isinstance(password, str)
            and MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH)


def hash_password(password):
    if not password_is_valid(password):
        raise ValueError("Passwords must contain 12 to 1024 characters.")
    return generate_password_hash(password, method="scrypt")


def check_password(password, stored):
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
        return False
    if not isinstance(stored, str):
        return False
    if LEGACY_HASH.fullmatch(stored):
        salt, digest = stored.split(":", 1)
        actual = hashlib.sha256((salt + password).encode()).hexdigest()
        return hmac.compare_digest(actual, digest)
    try:
        return check_password_hash(stored, password)
    except (ValueError, TypeError):
        return False


def session_stamp(password_hash, secret_key):
    """Changing a password invalidates previously issued sessions."""
    return hmac.new(secret_key.encode(), password_hash.encode(), "sha256").hexdigest()


def _fernet():
    key = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "")
    if not key:
        raise ValueError("Configure CREDENTIAL_ENCRYPTION_KEY before using SMTP.")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise ValueError("CREDENTIAL_ENCRYPTION_KEY must be a Fernet key.") from exc


def encrypt_credential(value):
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError("SMTP passwords must contain at most 256 characters.")
    return ENCRYPTED_PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt_credential(value):
    if not value or not value.startswith(ENCRYPTED_PREFIX):
        raise ValueError("Run flask --app app encrypt-smtp-passwords before sending mail.")
    try:
        return _fernet().decrypt(value[len(ENCRYPTED_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise ValueError("SMTP credential could not be decrypted with the configured key.") from exc


def smtp_destination(host, port):
    """Allow only operator-approved hosts, standard TLS ports and public IPs."""
    host = (host or "").strip().lower().rstrip(".")
    allowed = {entry.strip().lower().rstrip(".") for entry in
               os.environ.get("SMTP_ALLOWED_HOSTS", "").split(",") if entry.strip()}
    if not host or host not in allowed:
        raise ValueError("SMTP host must be listed in SMTP_ALLOWED_HOSTS by the operator.")
    if port not in (465, 587):
        raise ValueError("SMTP port must be 465 or 587.")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(address[4][0] for address in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("SMTP host must resolve exclusively to public addresses.")
    return host, ips


def open_smtp(host, port, timeout=15):
    host, ips = smtp_destination(host, port)
    context = ssl.create_default_context()

    def connect_pinned():
        last_error = None
        for ip in ips:
            try:
                return socket.create_connection((ip, port), timeout=timeout)
            except OSError as exc:
                last_error = exc
        raise last_error or OSError("SMTP connection failed.")

    # Connect to the validated address rather than resolving the hostname again.
    # Keep the original hostname for TLS certificate verification and SNI.
    class PinnedSMTP(smtplib.SMTP):
        def _get_socket(self, host, port, timeout):
            return connect_pinned()

    class PinnedSMTPSSL(smtplib.SMTP_SSL):
        def _get_socket(self, host, port, timeout):
            sock = connect_pinned()
            try:
                return context.wrap_socket(sock, server_hostname=host)
            except Exception:
                sock.close()
                raise

    if port == 465:
        return PinnedSMTPSSL(host, port, timeout=timeout, context=context)
    client = PinnedSMTP(host, port, timeout=timeout)
    try:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
    except Exception:
        client.close()
        raise
    return client

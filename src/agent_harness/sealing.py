"""Sealed payloads for owner commands.

An owner command travels through the collaboration service, which is a separate
trust domain from the machines it configures. Routing has to stay readable there
(target, command id), but the payload may hold things the service must not learn
(a local proxy address, a path, a credential reference). This module seals the
payload to the receiving device's key so only that machine can open it.

Construction: X25519 ECDH against a fresh ephemeral key per message, HKDF-SHA256
to a 32 byte key, AES-GCM with the envelope's routing fields as associated data,
so a sealed payload cannot be moved onto a different command or target.
"""

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

INFO = b"harness-owner-payload-v1"
MAX_PLAINTEXT = 64 * 1024
NONCE_BYTES = 12


def _encode(raw):
    return base64.b64encode(raw).decode("ascii")


def _decode(value, length=None):
    if not isinstance(value, str):
        raise ValueError("Invalid sealed payload")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except Exception:
        raise ValueError("Invalid sealed payload") from None
    if length is not None and len(raw) != length:
        raise ValueError("Invalid sealed payload")
    return raw


def generate_keypair():
    """A device-local encryption keypair. Only the public half is ever shared."""
    key = X25519PrivateKey.generate()
    return {
        "private_key": _encode(key.private_bytes_raw()),
        "public_key": _encode(key.public_key().public_bytes_raw()),
    }


def _associated_data(context):
    if context is None:
        return INFO
    if not isinstance(context, dict):
        raise ValueError("Sealing context must be an object")
    payload = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > 4096:
        raise ValueError("Sealing context is too large")
    return INFO + b"\x00" + payload


def seal(public_key, value, context=None):
    """Seal ``value`` so only the holder of the matching private key can read it."""
    recipient = X25519PublicKey.from_public_bytes(_decode(public_key, 32))
    if not isinstance(value, dict):
        raise ValueError("Sealed value must be an object")
    plaintext = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError("Sealed value is too large")
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(recipient)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(shared)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _associated_data(context))
    return {
        "version": 1,
        "epk": _encode(ephemeral.public_key().public_bytes_raw()),
        "nonce": _encode(nonce),
        "ciphertext": _encode(ciphertext),
    }


def open_sealed(private_key, sealed, context=None):
    """Open a sealed payload. Raises ValueError for anything malformed or forged."""
    if not isinstance(sealed, dict) or set(sealed) != {"version", "epk", "nonce", "ciphertext"}:
        raise ValueError("Invalid sealed payload")
    if sealed.get("version") != 1:
        raise ValueError("Unsupported sealed payload version")
    secret = X25519PrivateKey.from_private_bytes(_decode(private_key, 32))
    ephemeral = X25519PublicKey.from_public_bytes(_decode(sealed["epk"], 32))
    shared = secret.exchange(ephemeral)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(shared)
    try:
        plaintext = AESGCM(key).decrypt(
            _decode(sealed["nonce"], NONCE_BYTES),
            _decode(sealed["ciphertext"]),
            _associated_data(context),
        )
    except InvalidTag:
        raise ValueError("Sealed payload could not be verified") from None
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError("Sealed payload is too large")
    try:
        value = json.loads(plaintext.decode("utf-8"))
    except Exception:
        raise ValueError("Sealed payload is not an object") from None
    if not isinstance(value, dict):
        raise ValueError("Sealed payload is not an object")
    return value

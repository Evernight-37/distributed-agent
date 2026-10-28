"""Encrypted, offline account recovery packages.

The package contains the root identity only inside an authenticated ciphertext.
Import immediately derives a new child device identity; callers never persist the
recovered root material on the destination machine.
"""

from __future__ import annotations

import json
import os
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from agent_harness.account_identity import (
    account_id,
    approve_certificate,
    canonical,
    decode,
    encode,
    generate_identity,
    sign_envelope,
    verify_envelope,
)


RECOVERY_VERSION = 1
_AAD = b"agent-harness-recovery-v1"


def _key(password, salt):
    if not isinstance(password, str) or not password:
        raise ValueError("Recovery password is required")
    return Scrypt(salt=salt, length=32, n=65536, r=8, p=1).derive(password.encode("utf-8"))


def _validate_root(identity):
    if not isinstance(identity, dict) or identity.get("chain"):
        raise PermissionError("Only the root device can create a recovery package")
    if account_id(identity.get("public_key")) != identity.get("account_id"):
        raise PermissionError("Invalid root identity")
    decode(identity.get("private_key"), 32)
    verify_envelope(sign_envelope(identity, {"domain": "harness-recovery-root-proof-v1"}))


def create_recovery_package(identity, password, *, created_at=None):
    _validate_root(identity)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    payload = {
        "domain": "harness-recovery-package-v1",
        "recovery_id": secrets.token_hex(16),
        "created_at": float(time.time() if created_at is None else created_at),
        "root_identity": identity,
    }
    ciphertext = AESGCM(_key(password, salt)).encrypt(nonce, canonical(payload), _AAD)
    return {
        "version": RECOVERY_VERSION,
        "kdf": {"name": "scrypt", "n": 65536, "r": 8, "p": 1},
        "salt": encode(salt),
        "nonce": encode(nonce),
        "ciphertext": encode(ciphertext),
    }


def recover_device(package, password):
    if not isinstance(package, dict) or package.get("version") != RECOVERY_VERSION:
        raise ValueError("Unsupported recovery package")
    if package.get("kdf") != {"name": "scrypt", "n": 65536, "r": 8, "p": 1}:
        raise ValueError("Unsupported recovery package KDF")
    try:
        salt = decode(package["salt"], 16)
        nonce = decode(package["nonce"], 12)
        plaintext = AESGCM(_key(password, salt)).decrypt(nonce, decode(package["ciphertext"]), _AAD)
        payload = json.loads(plaintext)
        if (
            not isinstance(payload, dict)
            or payload.get("domain") != "harness-recovery-package-v1"
            or not isinstance(payload.get("recovery_id"), str)
            or len(payload["recovery_id"]) != 32
        ):
            raise ValueError("Invalid recovery package payload")
        root = payload["root_identity"]
        _validate_root(root)
    except (ValueError, PermissionError):
        raise
    except Exception:
        raise PermissionError("Unable to unlock recovery package") from None
    child = generate_identity()
    child.update(account_id=root["account_id"], chain=approve_certificate(root, child["public_key"]))
    verify_envelope(sign_envelope(child, {"domain": "harness-recovered-device-proof-v1"}), root["account_id"])
    return child

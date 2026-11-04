"""Decryption of Self-hosted LiveSync values (read side only).

Formats, from octagonal-wheels 0.1.47 (MIT) src/encryption/{hkdf,encryption,encryptionv3}.ts:

  %=<b64>   HKDF:   iv(12) | hkdf_salt(32) | AES-GCM(ct+tag); key = HKDF-SHA256(PBKDF2-SHA256(pass, pbkdf2_salt,
            310000), salt=hkdf_salt, info=b"");  pbkdf2_salt comes from the database's sync-parameters document
  %$<b64>   HKDF with the PBKDF2 salt carried inline: pbkdf2_salt(32) | iv(12) | hkdf_salt(32) | ct+tag
  %~<24 hex iv><b64>   V3: key = PBKDF2-SHA256(pass, sha256(pass + "fancySyncForYou!")[:16], 100000)
  %<32 hex iv><32 hex salt><b64>   V2: key = PBKDF2-SHA256(sha256(pass), salt, iterations); 16-byte IV
  ["<b64>","<iv hex>","<salt hex>"]   V1: as V2 but the plaintext is JSON-encoded

Iterations for V1/V2 are 100000, or with "dynamic iteration count" (15-len)*1000 + 121 - (15-len), where a passphrase
of 15 or more characters counts as len 15 for the first term only (see _iterations). Both schemes are tried.
"""

from __future__ import annotations

import base64
import binascii
import functools
import os
import hashlib
import json

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

HKDF_PREFIX = "%="
HKDF_SALTED_PREFIX = "%$"
V3_PREFIX = "%~"
V2_PREFIX = "%"
V1_PREFIX = "["
META_PREFIX = "/\\:"  # encrypted metadata in `path` (obfuscated entries): META_PREFIX + HKDF ciphertext

_PBKDF2_HKDF_ITERATIONS = 310_000


class DecryptError(Exception):
    """Wrong passphrase, wrong salt, damaged or unsupported data. Never carries the data."""


def b64d(text: str) -> bytes:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
    except (binascii.Error, ValueError):
        raise DecryptError("invalid base64") from None


def _pbkdf2(secret: bytes, salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(secret)


@functools.lru_cache(maxsize=16)
def _hkdf_master(passphrase: str, pbkdf2_salt: bytes) -> bytes:
    return _pbkdf2(passphrase.encode(), pbkdf2_salt, _PBKDF2_HKDF_ITERATIONS)


@functools.lru_cache(maxsize=4096)
def _hkdf_key(passphrase: str, pbkdf2_salt: bytes, hkdf_salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=hkdf_salt, info=b"").derive(_hkdf_master(passphrase, pbkdf2_salt))


@functools.lru_cache(maxsize=16)
def _legacy_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    return _pbkdf2(hashlib.sha256(passphrase.encode()).digest(), salt, iterations)


@functools.lru_cache(maxsize=2)
def _v3_key(passphrase: str) -> bytes:
    raw = passphrase.encode()
    salt = hashlib.sha256(raw + b"fancySyncForYou!").digest()[:16]
    return _pbkdf2(raw, salt, 100_000)


def _iterations(passphrase: str, dynamic: bool) -> int:
    if not dynamic:
        return 100_000
    n = 15 - len(passphrase)
    return (n if n > 0 else 0) * 1000 + 121 - n


def _gcm(key: bytes, iv: bytes, data: bytes) -> str:
    try:
        return AESGCM(key).decrypt(iv, data, None).decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError):
        raise DecryptError("cannot decrypt (wrong passphrase or salt, or damaged data)") from None


def decrypt_hkdf(value: str, passphrase: str, pbkdf2_salt: bytes | None) -> str:
    if value.startswith(HKDF_SALTED_PREFIX):
        raw = b64d(value[2:])
        if len(raw) < 32 + 12 + 32 + 16:
            raise DecryptError("truncated value")
        salt, iv, hsalt, ct = raw[:32], raw[32:44], raw[44:76], raw[76:]
        return _gcm(_hkdf_key(passphrase, salt, hsalt), iv, ct)
    if value.startswith(HKDF_PREFIX):
        if pbkdf2_salt is None:
            raise DecryptError("this value needs the database's PBKDF2 salt (sync parameters document not found)")
        raw = b64d(value[2:])
        if len(raw) < 12 + 32 + 16:
            raise DecryptError("truncated value")
        iv, hsalt, ct = raw[:12], raw[12:44], raw[44:]
        return _gcm(_hkdf_key(passphrase, pbkdf2_salt, hsalt), iv, ct)
    raise DecryptError("not an HKDF value")


def encrypt_hkdf(text: str, passphrase: str, pbkdf2_salt: bytes) -> str:
    """The "%=" format LiveSync writes (octagonal-wheels hkdf.ts encrypt): b64(iv(12) | hkdf_salt(32) | AES-GCM(text))."""
    iv, hkdf_salt = os.urandom(12), os.urandom(32)
    ct = AESGCM(_hkdf_key(passphrase, pbkdf2_salt, hkdf_salt)).encrypt(iv, text.encode("utf-8"), None)
    return HKDF_PREFIX + base64.b64encode(iv + hkdf_salt + ct).decode()


def _decrypt_v2(value: str, passphrase: str, dynamic: bool) -> str:
    if len(value) < 66 or value[65:66] == "%":  # a leading % in the payload would mean UTF-16 encoding, unsupported
        raise DecryptError("unsupported or truncated V2 value")
    try:
        iv, salt = bytes.fromhex(value[1:33]), bytes.fromhex(value[33:65])
    except ValueError:
        raise DecryptError("bad V2 header") from None
    return _gcm(_legacy_key(passphrase, salt, _iterations(passphrase, dynamic)), iv, b64d(value[65:]))


def _decrypt_v3(value: str, passphrase: str) -> str:
    try:
        iv = bytes.fromhex(value[2:26])
    except ValueError:
        raise DecryptError("bad V3 header") from None
    return _gcm(_v3_key(passphrase), iv, b64d(value[26:]))


def _decrypt_v1(value: str, passphrase: str, dynamic: bool) -> str:
    if not value.endswith("]"):
        raise DecryptError("damaged V1 value")
    parts = [p[1:-1] if p.startswith('"') else p for p in value[1:-1].split(",")]
    if len(parts) != 3:
        raise DecryptError("damaged V1 value")
    data, iv_hex, salt_hex = parts
    try:
        iv, salt = bytes.fromhex(iv_hex), bytes.fromhex(salt_hex)
    except ValueError:
        raise DecryptError("bad V1 header") from None
    text = _gcm(_legacy_key(passphrase, salt, _iterations(passphrase, dynamic)), iv, b64d(data))
    try:
        return json.loads(text)
    except ValueError:
        raise DecryptError("damaged V1 plaintext") from None


def decrypt(value: str, passphrase: str, pbkdf2_salt: bytes | None = None) -> str:
    """Decrypt any LiveSync value by its prefix. Older formats try both iteration schemes."""
    if value.startswith((HKDF_PREFIX, HKDF_SALTED_PREFIX)):
        return decrypt_hkdf(value, passphrase, pbkdf2_salt)
    if value.startswith(V3_PREFIX):
        return _decrypt_v3(value, passphrase)
    fn = _decrypt_v2 if value.startswith(V2_PREFIX) else _decrypt_v1 if value.startswith(V1_PREFIX) else None
    if fn is None:
        raise DecryptError("unrecognised encryption format")
    try:
        return fn(value, passphrase, False)
    except DecryptError:
        return fn(value, passphrase, True)


def looks_like_obfuscated_path(path: str) -> bool:
    """octagonal-wheels isPathProbablyObfuscated."""
    return path.startswith("%") and len(path) > 64


def encryption_kind(value: str) -> str:
    """Name the format of a value, for diagnostics."""
    for prefix, name in ((HKDF_SALTED_PREFIX, "hkdf-salted"), (HKDF_PREFIX, "hkdf"), (V3_PREFIX, "v3"), (V2_PREFIX, "v2"), (V1_PREFIX, "v1")):
        if value.startswith(prefix):
            return name
    return "none"

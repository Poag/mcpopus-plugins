"""Writes LiveSync-format documents (the inverse of the plugin's reader), following livesync-commonlib's
incomingEncryptHKDF / incomingEncryptV1 and octagonal-wheels' encrypt functions. The decrypt side of every
format is separately checked against vectors from the library's own tests and, in CI, against fixtures that
the real JavaScript code generates (tools/gen_fixtures.mjs)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import zlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

NOTES = {
    "Claude/Code/demo.md": "---\ntitle: Demo\ntags: [group/code, mcp]\n---\n# Demo\nLinks to [[Other Note]] and [text](Claude/Tech/server.md). Inline #inline-tag here.\n" + "Filler line for chunking. " * 40 + "\nEnd of demo.\n",
    "Claude/Tech/server.md": "# Server\nBack to [[demo]]. Unicode: café ☕ 日本語 🐙.\n```\n#not-a-tag\n```\n",
    "Other Note.md": "Plain root note with a #root tag and the word needle.\n",
    "Medical/Conditions/private.md": "# Private\nSensitive text, secret-medical-token, must never leave when Medical/ is blocked. See [[demo]].\n",
    "Daily/2026-11-04.md": "---\ntags:\n  - daily\n---\nToday needle.\n",
}
MTIME = 1_762_000_000_000


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def pbkdf2(secret: bytes, salt: bytes, n: int) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=n).derive(secret)


def hkdf_encrypt(text: str, passphrase: str, pbkdf2_salt: bytes, salted: bool = False) -> str:
    hsalt, iv = os.urandom(32), os.urandom(12)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=hsalt, info=b"").derive(pbkdf2(passphrase.encode(), pbkdf2_salt, 310_000))
    ct = AESGCM(key).encrypt(iv, text.encode(), None)
    return ("%$" + b64(pbkdf2_salt + iv + hsalt + ct)) if salted else ("%=" + b64(iv + hsalt + ct))


def v2_encrypt(text: str, passphrase: str, iterations: int = 100_000) -> str:
    salt, iv = os.urandom(16), os.urandom(16)
    ct = AESGCM(pbkdf2(hashlib.sha256(passphrase.encode()).digest(), salt, iterations)).encrypt(iv, text.encode(), None)
    return "%" + iv.hex() + salt.hex() + b64(ct)


def v1_encrypt(text: str, passphrase: str) -> str:
    salt, iv = os.urandom(16), os.urandom(16)
    ct = AESGCM(pbkdf2(hashlib.sha256(passphrase.encode()).digest(), salt, 100_000)).encrypt(iv, json.dumps(text).encode(), None)
    return f'["{b64(ct)}","{iv.hex()}","{salt.hex()}"]'


def v3_encrypt(text: str, passphrase: str) -> str:
    raw = passphrase.encode()
    key = pbkdf2(raw, hashlib.sha256(raw + b"fancySyncForYou!").digest()[:16], 100_000)
    iv = os.urandom(12)
    return "%~" + iv.hex() + b64(AESGCM(key).encrypt(iv, text.encode(), None))


def obfuscate_path_v2(path: str, passphrase: str) -> str:
    """octagonal-wheels obfuscatePath: salt and iv are derived from the path, so it is deterministic."""
    buf = hashlib.sha256(path.encode() + passphrase.encode()).digest()
    salt, iv = buf[:16], buf[16:32]
    ct = AESGCM(pbkdf2(hashlib.sha256(passphrase.encode()).digest(), salt, 100_000)).encrypt(iv, path.encode(), None)
    return "%" + iv.hex() + salt.hex() + b64(ct)


def compress(text: str) -> str:
    co = zlib.compressobj(8, zlib.DEFLATED, -15)
    return "\x0eLZ\x1d" + b64(co.compress(text.encode()) + co.flush())


def split(text: str, size: int = 300) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def build_vault(mode: str, passphrase: str = "writer-test-passphrase", notes: dict[str, str] | None = None) -> list[dict]:
    """mode: plain | hkdf (E2EE + obfuscated paths, mixed compression/%$) | v1 (V2/V3/V1 chunks + obfuscated paths)."""
    notes = notes or NOTES
    docs: list[dict] = []
    salt = os.urandom(32)
    for i, (path, text) in enumerate(notes.items(), 1):
        children, eden = [], {}
        for c, part in enumerate(split(text)):
            cid = ("h:" if mode == "plain" else "h:+") + sha(path + str(c))[:24]
            doc = {"_id": cid, "_rev": "1-" + sha(cid)[:32], "type": "leaf", "data": part}
            if mode == "plain":
                if i == 2:
                    doc["data"] = compress(part)
            elif mode == "e2ee":  # encrypted chunks, plain paths and ids (E2EE without "obfuscate properties")
                doc["data"] = hkdf_encrypt(part, passphrase, salt)
                doc["e_"] = True
            elif mode == "hkdf":
                if i == 1:
                    doc["data"] = hkdf_encrypt(compress(part), passphrase, salt)  # compress, then encrypt
                elif i == 2:
                    doc["data"] = compress(hkdf_encrypt(part, passphrase, salt))  # encrypt, then compress
                elif i == 3:
                    doc["data"] = hkdf_encrypt(part, passphrase, salt, salted=True)
                else:
                    doc["data"] = hkdf_encrypt(part, passphrase, salt)
                doc["e_"] = True
            else:
                doc["data"] = (v1_encrypt, v3_encrypt, v2_encrypt, v2_encrypt, v2_encrypt)[i - 1](part, passphrase)
                doc["e_"] = True
            if i == 5 and c == 0 and mode not in ("v1", "e2ee"):
                eden[cid] = {"data": part, "epoch": 1}  # chunk carried inline, never its own document
            else:
                docs.append(doc)
            children.append(cid)
        meta = {"_id": path.lower(), "_rev": "1-" + sha(path)[:32], "type": "newnote" if (mode == "plain" and i == 3) else "plain",
                "path": path, "children": children, "ctime": MTIME + i - 5000, "mtime": MTIME + i * 1000, "size": len(text.encode()), "eden": eden}
        if mode == "plain" and i == 3:  # binary-typed entry: base64 per chunk
            meta["children"] = []
            for c, part in enumerate(split(text)):
                cid = "h:" + sha(path + "b" + str(c))[:24]
                docs.append({"_id": cid, "_rev": "1-aa", "type": "leaf", "data": b64(part.encode())})
                meta["children"].append(cid)
        if mode == "hkdf":
            props = json.dumps({"path": path, "mtime": meta["mtime"], "ctime": meta["ctime"], "size": meta["size"], "children": children})
            meta.update(_id="f:" + sha(path), path="/\\:" + hkdf_encrypt(props, passphrase, salt), mtime=0, ctime=0, size=0, children=[])
            if eden:
                meta["eden"] = {"h:++encrypted-hkdf": {"data": hkdf_encrypt(json.dumps(eden), passphrase, salt), "epoch": 999999}}
        elif mode == "v1":
            meta.update(_id="f:" + sha(path), path=obfuscate_path_v2(path, passphrase))
        docs.append(meta)
    docs.append({"_id": "gone.md", "_rev": "2-aa", "type": "plain", "path": "Gone.md", "children": [], "ctime": 1, "mtime": 2, "size": 0, "deleted": True, "eden": {}})
    docs.append({"_id": "i:.obsidian/app.json", "_rev": "1-aa", "type": "plain", "path": "i:.obsidian/app.json", "children": [], "ctime": 1, "mtime": 1, "size": 2, "eden": {}})
    docs.append({"_id": "image.png", "_rev": "1-bb", "type": "newnote", "path": "image.png", "children": [], "ctime": 1, "mtime": 1, "size": 5, "eden": {}})
    docs.append({"_id": "obsydian_livesync_version", "_rev": "1-cc", "type": "versioninfo", "version": 12})
    if mode in ("hkdf", "e2ee"):
        docs.append({"_id": "_local/obsidian_livesync_sync_parameters", "type": "sync-parameters", "protocolVersion": 2, "pbkdf2salt": b64(salt)})
    return docs

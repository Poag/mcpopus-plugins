"""Building LiveSync documents for an unencrypted vault (the write side).

Shapes from livesync-commonlib `putDBEntryInternal`/`createChunks` (src/managers/EntryManager/EntryManagerImpls.ts):

  chunk  {"_id": "h:<id>", "type": "leaf", "data": <piece>}
  note   {"_id", "path", "ctime", "mtime", "size", "type": "plain", "children": [chunk ids], "eden": {}}   (+ "_rev" when replacing)
  delete the same note document with "deleted": true and a new "mtime" (LiveSync's default soft delete)

Chunk ids are content hashes, so equal text always gets the same id. The splitting of text into pieces is the writer's
choice (readers follow `children`); this one cuts at line ends into pieces of at most CHUNK_CHARS characters.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from .xxh64 import xxh64

CHUNK_CHARS = 1000
CHUNK_BYTES = 24_000  # binary (newnote) entries: bytes per chunk before base64
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_LINES = re.compile(r"[^\n]*\n|[^\n]+")


def js_length(s: str) -> int:
    """String.length in JavaScript: UTF-16 code units."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def to_base36(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out or "0"


def chunk_id(piece: str) -> str:
    """XXHash64 manager, no encryption: h64(`${piece}-${piece.length}`).toString(36)."""
    return "h:" + to_base36(xxh64(f"{piece}-{js_length(piece)}".encode("utf-8", "surrogatepass")))


def split(text: str) -> list[str]:
    pieces: list[str] = []
    cur, size = [], 0
    for line in _LINES.findall(text):
        while len(line) > CHUNK_CHARS:  # a very long line: cut it (by code point, so a surrogate pair is never split)
            if cur:
                pieces.append("".join(cur))
                cur, size = [], 0
            pieces.append(line[:CHUNK_CHARS])
            line = line[CHUNK_CHARS:]
        if size + len(line) > CHUNK_CHARS and cur:
            pieces.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        pieces.append("".join(cur))
    return pieces


def note_id(path: str, *, case_sensitive: bool, nfc: bool) -> str:
    """path2id_base without obfuscation: optional NFC, lower-case unless case-sensitive, a leading "_" becomes "/_"."""
    p = unicodedata.normalize("NFC", path) if nfc else path
    if not case_sensitive:
        p = p.lower()
    return "/" + p if p.startswith("_") else p


def id_key(passphrase: str) -> bytes:
    return hashlib.sha256(b"mcpopus-chunk-id\0" + passphrase.encode("utf-8")).digest()


def secret_chunk_id(piece: str, key: bytes) -> str:
    """Id of an encrypted chunk: "h:+" and a keyed 64-bit hash of the text. LiveSync readers only follow `children` and
    use the "h:+" prefix to know the chunk is encrypted, so the hash need not match LiveSync's own; it is keyed so that
    ids reveal nothing about the text to someone without the passphrase."""
    mac = hmac.new(key, piece.encode("utf-8", "surrogatepass"), hashlib.sha256).digest()[:8]
    return "h:+" + to_base36(int.from_bytes(mac, "big"))


def build(path: str, text: str, doc_id: str, *, ctime: int, mtime: int, encrypt: Callable[[str], str] | None = None,
          key: bytes | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pieces = split(text)
    if encrypt is not None:
        ids = [secret_chunk_id(p, key or b"") for p in pieces]
        chunks = [{"_id": i, "type": "leaf", "data": encrypt(p), "e_": True} for i, p in zip(ids, pieces)]
    else:
        ids = [chunk_id(p) for p in pieces]
        chunks = [{"_id": i, "type": "leaf", "data": p} for i, p in zip(ids, pieces)]
    meta = {"_id": doc_id, "path": path, "ctime": ctime, "mtime": mtime, "size": len(text.encode("utf-8")),
            "type": "plain", "children": ids, "eden": {}}
    return chunks, meta


def split_bytes(data: bytes) -> list[bytes]:
    """Cut at newlines into slices of at most CHUNK_BYTES. Readers decode every chunk alone and join the bytes, so any cut is valid."""
    pieces: list[bytes] = []
    start = 0
    while start < len(data):
        end = min(start + CHUNK_BYTES, len(data))
        if end < len(data):
            cut = data.rfind(b"\n", start, end)
            if cut > start:
                end = cut + 1
        pieces.append(data[start:end])
        start = end
    return pieces


def build_binary(path: str, data: bytes, doc_id: str, *, ctime: int, mtime: int, encrypt: Callable[[str], str] | None = None,
                 key: bytes | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A non-text file as LiveSync stores it: type "newnote", every chunk the base64 of a slice of the bytes, size in bytes.
    The chunk id hashes the stored (base64) text, exactly as for any chunk."""
    texts = [base64.b64encode(p).decode("ascii") for p in split_bytes(data)]
    if encrypt is not None:
        ids = [secret_chunk_id(t, key or b"") for t in texts]
        chunks = [{"_id": i, "type": "leaf", "data": encrypt(t), "e_": True} for i, t in zip(ids, texts)]
    else:
        ids = [chunk_id(t) for t in texts]
        chunks = [{"_id": i, "type": "leaf", "data": t} for i, t in zip(ids, texts)]
    meta = {"_id": doc_id, "path": path, "ctime": ctime, "mtime": mtime, "size": len(data), "type": "newnote", "children": ids, "eden": {}}
    return chunks, meta

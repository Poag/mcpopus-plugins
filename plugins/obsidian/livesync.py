"""Reading Self-hosted LiveSync documents: metadata, chunks, compression, eden, encryption.

Document model (livesync-commonlib 0.3.x src/common/models/db.type.ts and src/pouchdb/encryption.ts):

* A note is a metadata document (type "plain", "newnote" or legacy "notes") with `path`, `children` (chunk ids),
  `ctime`, `mtime`, `size`, optional `deleted`, and `eden` (chunks carried inline: {chunk_id: {data, epoch}}).
* A chunk is a "leaf" document {_id: "h:..." or "h:+..." when encrypted, data, e_?}. The text of a note is the
  concatenation of its chunks' data in `children` order.
* With "obfuscate properties" the metadata id is "f:<hash>" and `path` holds "/\\:" + an HKDF ciphertext of a JSON
  object {path, mtime, ctime, size, children} (the cleartext fields are zeroed); older vaults hold the path alone,
  encrypted in the V2 format.
* `eden` is replaced by one encrypted entry when encryption is on ("h:++encrypted-hkdf" or "h:++encrypted").
* Chunk data may be compressed: "\\x0eLZ\\x1d" + base64(raw DEFLATE); a leading "~" inside means binary.
  Whether compression or encryption is applied first on disk is not stated in the source I read, so the reader
  peels whichever layer it finds, up to three times.
"""

from __future__ import annotations

import base64
import json
import unicodedata
import zlib
from dataclasses import dataclass, field
from typing import Any

from .crypto import META_PREFIX, DecryptError, decrypt, encryption_kind, looks_like_obfuscated_path

NOTE_TYPES = ("plain", "newnote", "notes")
COMPRESSED_MARK = "\x0eLZ\x1d"
EDEN_ENCRYPTED = "h:++encrypted"
EDEN_ENCRYPTED_HKDF = "h:++encrypted-hkdf"
NOT_NOTE_PREFIXES = ("i:", "ix:", "ps:", "ch:")  # LiveSync internal files and customisation sync, never notes
SYNC_PARAMETERS_ID = "_local/obsidian_livesync_sync_parameters"


class LiveSyncError(Exception):
    """A document could not be read. The message names what, never the data."""


@dataclass
class NoteMeta:
    doc_id: str
    rev: str
    path: str
    mtime: int
    ctime: int
    size: int
    children: list[str]
    eden: dict[str, str] = field(default_factory=dict)  # chunk id -> data
    kind: str = "plain"
    legacy_data: str | None = None
    obfuscated: bool = False


def nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def decompress(data: str) -> str:
    body = data[len(COMPRESSED_MARK):]
    binary = body.startswith("~")
    if binary:
        body = body[1:]
    if not body:
        return ""
    try:
        raw = zlib.decompress(base64.b64decode(body + "=" * (-len(body) % 4)), -15)
    except (zlib.error, ValueError):
        raise LiveSyncError("a compressed chunk is damaged") from None
    return base64.b64encode(raw).decode() if binary else raw.decode("utf-8", "replace")


class Codec:
    def __init__(self, passphrase: str | None, pbkdf2_salt: bytes | None) -> None:
        self.passphrase = passphrase or None
        self.pbkdf2_salt = pbkdf2_salt

    def _dec(self, value: str) -> str:
        if not self.passphrase:
            raise LiveSyncError("the vault is encrypted but no passphrase is configured")
        try:
            return decrypt(value, self.passphrase, self.pbkdf2_salt)
        except DecryptError as e:
            raise LiveSyncError(str(e)) from None

    # ------------------------------------------------------------- metadata
    def is_note_candidate(self, doc: dict[str, Any]) -> bool:
        _id = doc.get("_id", "")
        return doc.get("type") in NOTE_TYPES and not _id.startswith(NOT_NOTE_PREFIXES) and not _id.startswith("_")

    def decode_meta(self, doc: dict[str, Any]) -> NoteMeta | None:
        """Return None for documents that are not (or no longer) notes: internal files, deleted entries."""
        if not self.is_note_candidate(doc):
            return None
        path = doc.get("path", "")
        mtime, ctime, size = doc.get("mtime", 0), doc.get("ctime", 0), doc.get("size", 0)
        children = list(doc.get("children") or [])
        obfuscated = False
        if path.startswith(META_PREFIX):
            props = json.loads(self._dec(path[len(META_PREFIX):]))
            path, mtime, ctime, size = props["path"], props.get("mtime", 0), props.get("ctime", 0), props.get("size", 0)
            children = list(props.get("children") or [])
            obfuscated = True
        elif looks_like_obfuscated_path(path):
            path = self._dec(path)
            obfuscated = True
        if not path or path.startswith(NOT_NOTE_PREFIXES):
            return None
        if path.startswith("/"):
            path = path[1:]  # ids that begin with "_" are stored with a leading "/"
        if doc.get("deleted") or doc.get("_deleted"):
            return None
        return NoteMeta(
            doc_id=doc["_id"], rev=doc.get("_rev", ""), path=path, mtime=int(mtime), ctime=int(ctime), size=int(size),
            children=children, eden=self._eden(doc.get("eden") or {}), kind=doc["type"],
            legacy_data=(doc["data"] if isinstance(doc.get("data"), str) else "".join(doc["data"])) if doc["type"] == "notes" and "data" in doc else None,
            obfuscated=obfuscated,
        )

    def _eden(self, eden: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        merged = dict(eden)
        for key in (EDEN_ENCRYPTED_HKDF, EDEN_ENCRYPTED):
            if key in merged:
                inner = json.loads(self._dec(merged.pop(key)["data"]))
                merged.update(inner)
        for chunk_id, item in merged.items():
            data = item.get("data") if isinstance(item, dict) else None
            if isinstance(data, str):
                out[chunk_id] = data
        return out

    # --------------------------------------------------------------- chunks
    def decode_chunk(self, doc: dict[str, Any]) -> str:
        data = doc.get("data")
        if not isinstance(data, str):
            raise LiveSyncError("a chunk has no data")
        encrypted = bool(doc.get("e_"))
        for _ in range(3):
            if data.startswith(COMPRESSED_MARK):
                data = decompress(data)
            elif encrypted:
                data = self._dec(data)
                encrypted = False
            else:
                break
        return data

    def assemble(self, meta: NoteMeta, chunks: dict[str, dict[str, Any]]) -> str:
        """The note text. Raises LiveSyncError if a chunk is missing or unreadable."""
        if meta.kind == "notes":
            return meta.legacy_data or ""
        parts: list[str] = []
        for chunk_id in meta.children:
            if chunk_id in meta.eden:
                parts.append(meta.eden[chunk_id])
            elif chunk_id in chunks:
                parts.append(self.decode_chunk(chunks[chunk_id]))
            else:
                raise LiveSyncError("a chunk of this note is missing from the database")
        if meta.kind == "newnote":  # binary entries are base64 per chunk
            try:
                return b"".join(base64.b64decode(p + "=" * (-len(p) % 4)) for p in parts).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise LiveSyncError("this entry is binary, not text") from None
        return "".join(parts)

    # ---------------------------------------------------------- diagnostics
    def classify(self, docs: list[dict[str, Any]]) -> dict[str, Any]:
        """Counts only; used by the vault_status tool to tell the owner how the vault is stored."""
        ids = {"plain": 0, "obfuscated": 0}
        enc: dict[str, int] = {}
        compressed = 0
        for d in docs:
            t = d.get("type")
            if t in NOTE_TYPES and not d.get("_id", "").startswith(NOT_NOTE_PREFIXES):
                ids["obfuscated" if d["_id"].startswith("f:") else "plain"] += 1
            elif t == "leaf":
                kind = "none"
                if d.get("e_") and isinstance(d.get("data"), str):
                    kind = encryption_kind(d["data"])
                    if d["data"].startswith(COMPRESSED_MARK):
                        kind = "compressed-then-encrypted"
                elif isinstance(d.get("data"), str) and d["data"].startswith(COMPRESSED_MARK):
                    compressed += 1
                enc[kind] = enc.get(kind, 0) + 1
        return {"entry_ids": ids, "chunk_encryption": enc, "compressed_unencrypted_chunks": compressed}

"""The vault: an in-memory index of notes over a LiveSync CouchDB, read through the changes feed.

Nothing is written to CouchDB. Note text is fetched on demand, decoded in a worker thread (decryption is CPU work)
and kept in a size-bounded cache; it is never written to disk.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from . import writer
from .couch import Conflict, CouchDB, CouchError
from . import crypto
from .livesync import COMPRESSED_MARK, SYNC_PARAMETERS_ID, NOTE_TYPES, Codec, LiveSyncError, NoteMeta, nfc
from .parse import Parsed, parse
from .paths import FolderPolicy, PathError, is_note_path, validate_note_path

VERSION_DOC = "obsydian_livesync_version"
KNOWN_FEATURES = {"encrypted-internal-metadata-v1", "nfc-path-identity-v1"}  # neither changes how plain notes are written
MAX_KNOWN_VERSION = 13

log = logging.getLogger("mcpopus.plugin.obsidian")
REFRESH_MIN_INTERVAL = 1.0
SELECTOR = {"type": {"$in": list(NOTE_TYPES)}}


class WriteRefused(Exception):
    """Writing is not possible or not safe right now. The message says why and is safe to show."""


@dataclass
class VaultStats:
    notes_total: int = 0
    notes_visible: int = 0
    undecodable: int = 0
    seq: str = ""
    last_refresh: float = 0.0


class Vault:
    def __init__(self, couch: CouchDB, passphrase: str | None, policy: FolderPolicy, *, max_note_bytes: int,
                 max_scan_notes: int, cache_bytes: int = 32 * 1024 * 1024, writable: FolderPolicy | None = None,
                 max_write_bytes: int = 1_000_000) -> None:
        self.couch, self.passphrase, self.policy = couch, passphrase, policy
        self.writable = writable or FolderPolicy()  # no readable folders listed means nothing is writable
        self.max_write_bytes = max_write_bytes
        self._write_lock = asyncio.Lock()
        self._gate_cache: tuple[float, str | None, bool] | None = None
        self.write_mode: str | None = None  # "plain" or "hkdf", set by the write gate
        self.max_note_bytes, self.max_scan_notes, self.cache_limit = max_note_bytes, max_scan_notes, cache_bytes
        self.codec = Codec(passphrase, None)
        self.notes: dict[str, NoteMeta] = {}  # NFC path -> meta
        self._by_id: dict[str, str] = {}
        self.stats = VaultStats()
        self._seq: str | int = 0
        self._lock = asyncio.Lock()
        self._ready = False
        self._text: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._text_bytes = 0
        self._parsed: dict[tuple[str, str], Parsed] = {}
        self.salt_found = False

    # ------------------------------------------------------------------ sync
    async def _load_salt(self) -> None:
        doc = await self.couch.get_local(SYNC_PARAMETERS_ID)
        if doc and doc.get("pbkdf2salt"):
            try:
                self.codec.pbkdf2_salt = base64.b64decode(doc["pbkdf2salt"])
                self.salt_found = True
            except ValueError:
                log.warning("the sync parameters document has an unreadable PBKDF2 salt")

    async def ensure_ready(self) -> None:
        if not self._ready:
            async with self._lock:
                if not self._ready:
                    if self.passphrase:
                        await self._load_salt()
                    self._ready = True
        await self.refresh()

    async def refresh(self, force: bool = False) -> None:
        async with self._lock:
            if not force and time.monotonic() - self.stats.last_refresh < REFRESH_MIN_INTERVAL and self.stats.last_refresh:
                return
            while True:
                res = await self.couch.changes(self._seq, SELECTOR, 500)
                batch = res.get("results", [])
                ops = await asyncio.to_thread(self._decode_batch, batch)
                for kind, key, meta in ops:
                    if kind == "remove":
                        self._remove_id(key)
                    else:
                        self._remove_id(meta.doc_id)
                        self.notes[nfc(meta.path)] = meta
                        self._by_id[meta.doc_id] = nfc(meta.path)
                self._seq = res.get("last_seq", self._seq)
                if len(batch) < 500:
                    break
            self.stats.seq = str(self._seq)[:40]
            self.stats.last_refresh = time.monotonic()
            self.stats.notes_total = len(self.notes)
            self.stats.notes_visible = sum(1 for m in self.notes.values() if self.policy.allows_path(m.path))

    def _remove_id(self, doc_id: str) -> None:
        key = self._by_id.pop(doc_id, None)
        if key is not None:
            self.notes.pop(key, None)

    def _decode_batch(self, batch: list[dict[str, Any]]) -> list[tuple[str, str, NoteMeta | None]]:
        ops: list[tuple[str, str, NoteMeta | None]] = []
        for change in batch:
            doc = change.get("doc")
            if change.get("deleted") or not doc:
                ops.append(("remove", change["id"], None))
                continue
            try:
                meta = self.codec.decode_meta(doc)
            except (LiveSyncError, KeyError, ValueError, TypeError):
                self.stats.undecodable += 1
                ops.append(("remove", change["id"], None))
                continue
            if meta is None or not is_note_path(meta.path):
                ops.append(("remove", change["id"], None))
            else:
                ops.append(("upsert", meta.doc_id, meta))
        return ops

    # ----------------------------------------------------------------- reads
    def visible(self) -> list[NoteMeta]:
        return [m for m in self.notes.values() if self.policy.allows_path(m.path)]

    def find(self, path: str) -> NoteMeta:
        validate_note_path(path)
        meta = self.notes.get(nfc(path))
        if meta is None:
            meta = next((m for k, m in self.notes.items() if k.casefold() == nfc(path).casefold()), None)
        # Blocked and missing notes look the same.
        if meta is None or not self.policy.allows_path(meta.path):
            raise PathError("No such note.")
        return meta

    async def texts(self, metas: list[NoteMeta]) -> dict[str, str]:
        """Text of each note (keyed by path). Notes that cannot be read are left out."""
        out: dict[str, str] = {}
        need: list[NoteMeta] = []
        for m in metas:
            cached = self._text.get((m.doc_id, m.rev))
            if cached is not None:
                self._text.move_to_end((m.doc_id, m.rev))
                out[m.path] = cached
            elif m.size and m.size > self.max_note_bytes:
                continue
            else:
                need.append(m)
        if need:
            ids = sorted({c for m in need for c in m.children if c not in m.eden})
            chunks = await self.couch.bulk_get(ids)
            for m, text in zip(need, await asyncio.to_thread(self._assemble_all, need, chunks)):
                if text is not None:
                    out[m.path] = text
                    self._remember((m.doc_id, m.rev), text)
        return out

    def _assemble_all(self, metas: list[NoteMeta], chunks: dict[str, dict[str, Any]]) -> list[str | None]:
        res: list[str | None] = []
        for m in metas:
            try:
                res.append(self.codec.assemble(m, chunks))
            except (LiveSyncError, ValueError):
                self.stats.undecodable += 1
                res.append(None)
        return res

    def _remember(self, key: tuple[str, str], text: str) -> None:
        self._text[key] = text
        self._text_bytes += len(text)
        while self._text_bytes > self.cache_limit and self._text:
            _, old = self._text.popitem(last=False)
            self._text_bytes -= len(old)

    async def read(self, path: str) -> tuple[NoteMeta, str]:
        meta = self.find(path)
        if meta.size and meta.size > self.max_note_bytes:
            raise PathError("That note is larger than the configured limit.")
        got = await self.texts([meta])
        if meta.path not in got:
            raise LiveSyncError("That note could not be read from the database (missing chunk or wrong passphrase).")
        return meta, got[meta.path]

    async def parsed(self, metas: list[NoteMeta]) -> dict[str, Parsed]:
        """Tags and links for each note, computed from its text and cached by revision."""
        metas = metas[: self.max_scan_notes]
        texts = await self.texts([m for m in metas if (m.doc_id, m.rev) not in self._parsed])
        for m in metas:
            if m.path in texts:
                self._parsed[(m.doc_id, m.rev)] = parse(texts[m.path])
        return {m.path: self._parsed[(m.doc_id, m.rev)] for m in metas if (m.doc_id, m.rev) in self._parsed}

    # ---------------------------------------------------------- diagnostics
    async def diagnose(self) -> dict[str, Any]:
        info = await self.couch.info()
        notes = list(self.notes.values())
        pseudo = [{"type": "plain", "_id": m.doc_id} for m in notes]
        chunk_ids = [c for m in notes[:20] for c in m.children[:2] if c not in m.eden]
        chunks = list((await self.couch.bulk_get(chunk_ids)).values()) if chunk_ids else []
        report = await asyncio.to_thread(self.codec.classify, pseudo + chunks)
        report["sampled_chunks"] = len(chunks)
        report["database"] = {"doc_count": info.get("doc_count"), "reachable": True}
        report["sync_parameters_found"] = self.salt_found
        report["passphrase_configured"] = bool(self.passphrase)
        report["writes"] = {"writable_folders": list(self.writable.readable), "database_gate": (await self.write_gate(force=True)) or "ok", "mode": self.write_mode}
        return report

    # ---------------------------------------------------------------- writes
    def can_write_path(self, path: str) -> bool:
        return bool(self.writable.readable) and self.writable.allows_path(path) and self.policy.allows_path(path)

    def check_writable(self, path: str) -> None:
        validate_note_path(path)
        if not self.can_write_path(path):
            raise WriteRefused("Writing is not enabled for that folder.")

    def _scheme(self) -> tuple[bool, bool] | None:
        """(case_sensitive, nfc) as this vault derives note ids, learned from existing notes; None if it cannot be told."""
        metas = [m for m in self.notes.values() if not m.obfuscated]
        if not any(m.path != m.path.lower() or m.path != nfc(m.path) for m in metas):
            return None
        best, best_n = None, -1
        for cs in (False, True):
            for use_nfc in (False, True):
                n = sum(writer.note_id(m.path, case_sensitive=cs, nfc=use_nfc) == m.doc_id for m in metas)
                if n > best_n:
                    best, best_n = (cs, use_nfc), n
        return best if best_n >= max(1, int(len(metas) * 0.99)) else None

    async def write_gate(self, force: bool = False) -> str | None:
        """None when the database is one this plugin can safely write to, else the reason it cannot. Cached for a minute."""
        now = time.monotonic()
        if not force and self._gate_cache and now - self._gate_cache[0] < 60:
            return self._gate_cache[1]
        reason = await self._compute_gate()
        self._gate_cache = (now, reason, True)
        return reason

    async def _compute_gate(self) -> str | None:
        doc = await self.couch.get_doc(VERSION_DOC)
        if doc is None:
            return "this database has no LiveSync version document, so it is not safe to write to"
        version, feats = doc.get("version"), doc.get("used_features") or []
        if not isinstance(version, int) or version > MAX_KNOWN_VERSION:
            return f"this database is a newer LiveSync generation ({version}) than this plugin understands"
        unknown = [f for f in feats if f not in KNOWN_FEATURES]
        if unknown:
            return "this database uses LiveSync features this plugin does not know (" + ", ".join(str(u)[:40] for u in unknown[:5]) + ")"
        metas = list(self.notes.values())
        if not metas:
            return "the vault has no notes to learn its naming from, so writing is not enabled"
        if any(m.obfuscated for m in metas):
            return "this vault uses obfuscated paths; writing is not supported for obfuscated vaults in this version"
        sample = [c for m in metas[:8] for c in m.children[:2] if c not in m.eden]
        chunks = list((await self.couch.bulk_get(sample)).values()) if sample else []
        if not chunks:
            return "cannot look at any stored chunk to tell whether the vault is encrypted"
        kinds = {("none" if not d.get("e_") else "compressed" if str(d.get("data", "")).startswith(COMPRESSED_MARK)
                  else crypto.encryption_kind(str(d.get("data", "")))) for d in chunks}
        if kinds == {"none"}:
            self.write_mode = "plain"
        elif kinds <= {"hkdf", "hkdf-salted"}:
            if not self.passphrase:
                return "this vault is encrypted but no passphrase is configured"
            if self.codec.pbkdf2_salt is None:
                return "this vault is encrypted but its PBKDF2 salt (sync parameters document) was not found"
            self.write_mode = "hkdf"
        else:
            return "this vault stores chunks in a layout writing does not support (" + ", ".join(sorted(kinds)) + "); reading still works"
        if self._scheme() is None:
            return "cannot tell how this vault names notes (no mixed-case note names to learn from)"
        return None

    async def _prepare_write(self, path: str) -> None:
        self.check_writable(path)
        await self.ensure_ready()
        reason = await self.write_gate()
        if reason:
            raise WriteRefused(f"Writing is not available: {reason}.")

    def _existing(self, path: str) -> NoteMeta | None:
        key = nfc(path)
        meta = self.notes.get(key)
        return meta or next((m for k, m in self.notes.items() if k.casefold() == key.casefold()), None)

    async def put_note(self, path: str, text: str, *, base_rev: str | None = None, must_exist: bool = False,
                       must_not_exist: bool = False, ctime: int | None = None) -> NoteMeta:
        """Create or replace a note. With base_rev, only if the note is still at the revision that was read."""
        if len(text.encode("utf-8")) > self.max_write_bytes:
            raise WriteRefused("That note is larger than the configured write limit.")
        async with self._write_lock:
            await self._prepare_write(path)
            await self.refresh(force=True)
            existing = self._existing(path)
            if must_exist and existing is None:
                raise PathError("No such note.")
            if must_not_exist and existing is not None:
                raise PathError("A note already exists at that path.")
            if base_rev is not None and (existing is None or existing.rev != base_rev):
                raise Conflict("the note was changed by someone else since it was read; read it again and retry")
            if existing:
                doc_id = existing.doc_id
            else:
                cs, use_nfc = self._scheme() or (False, False)
                doc_id = writer.note_id(path, case_sensitive=cs, nfc=use_nfc)
            cur = await self.couch.get_doc(doc_id)
            if existing is None and cur and not (cur.get("deleted") or cur.get("_deleted")):
                raise PathError("Something else already uses that name.")
            now = int(time.time() * 1000)
            created = existing.ctime if existing else ctime or (cur or {}).get("ctime") or now
            enc = key = None
            if self.write_mode == "hkdf":
                salt, phrase = self.codec.pbkdf2_salt, self.passphrase or ""
                enc, key = (lambda piece: crypto.encrypt_hkdf(piece, phrase, salt)), writer.id_key(phrase)
            chunks, meta = await asyncio.to_thread(
                lambda: writer.build(existing.path if existing else path, text, doc_id, ctime=int(created), mtime=now, encrypt=enc, key=key))
            if enc is not None:  # never send encrypted data we could not read back ourselves
                back = await asyncio.to_thread(lambda: "".join(self.codec.decode_chunk(c) for c in chunks))
                if back != text:
                    raise WriteRefused("Writing is not available: an internal check of the encrypted data failed.")
            if cur:
                meta["_rev"] = cur["_rev"]
            unique = list({c["_id"]: c for c in chunks}.values())
            present = await self.couch.existing_ids([c["_id"] for c in unique])
            await self.couch.bulk_write([c for c in unique if c["_id"] not in present])
            await self.couch.put_doc(meta)
        await self.refresh(force=True)
        return self._existing(path)  # type: ignore[return-value]

    async def soft_delete(self, path: str, *, base_rev: str | None = None) -> None:
        """LiveSync's default delete: the note document stays, marked deleted, so every device removes the file."""
        async with self._write_lock:
            await self._prepare_write(path)
            await self.refresh(force=True)
            existing = self._existing(path)
            if existing is None:
                raise PathError("No such note.")
            if base_rev is not None and existing.rev != base_rev:
                raise Conflict("the note was changed by someone else since it was read; read it again and retry")
            cur = await self.couch.get_doc(existing.doc_id)
            if cur is None:
                raise PathError("No such note.")
            cur["deleted"] = True
            cur["mtime"] = int(time.time() * 1000)
            await self.couch.put_doc(cur)
        await self.refresh(force=True)

    async def move(self, src: str, dst: str) -> NoteMeta:
        self.check_writable(src)
        self.check_writable(dst)
        meta, text = await self.read(src)
        new = await self.put_note(dst, text, must_not_exist=True, ctime=meta.ctime)
        try:
            await self.soft_delete(src, base_rev=meta.rev)
        except (CouchError, WriteRefused, PathError):
            raise WriteRefused("The note was copied to the new path, but the original could not be deleted. Check both.") from None
        return new

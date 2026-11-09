"""obsidian: read-only Obsidian vault over a Self-hosted LiveSync CouchDB. Design: docs/DESIGN.md."""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from urllib.parse import quote

from mcpopus.plugin import BaseModel, Field, Plugin, PluginHost, SecretStr, ToolError, tool
from pydantic import field_validator

from .bases import base_links, parse_base, summarise
from .couch import Conflict, CouchDB, CouchError
from .livesync import LiveSyncError, NoteMeta
from .parse import link_target_matches, snippet
from .paths import FolderPolicy, PathError, validate_folder
from .vault import Vault, WriteRefused


class Config(BaseModel):
    couchdb_url: str = Field(description="CouchDB base URL, e.g. http://couchdb:5984 (no credentials in the URL)")
    database: str = Field(description="The LiveSync database. It must already exist; the plugin only reads it")
    username: str = Field("", description="CouchDB user (read access is enough)")
    password: SecretStr | None = Field(None, description="CouchDB password")
    passphrase: SecretStr | None = Field(None, description="LiveSync end-to-end encryption passphrase, if the vault uses one")
    vault_name: str = Field("Vault", max_length=80, description="Vault name for obsidian:// links")
    readable_folders: list[str] = Field([], description="Only these folders can be read (one per line). Empty means the whole vault")
    blocked_folders: list[str] = Field([], description="These folders are never readable, whatever else is set (one per line), e.g. Medical")
    writable_folders: list[str] = Field([], description="Folders where write tools may create, change, move and delete notes (one per line). Empty means nothing is writable. The host policy must also enable the write tools by name")
    max_write_bytes: int = Field(1_000_000, ge=1_000, le=20_000_000, description="Largest note a write may store")
    instructions: str = Field("", max_length=4000, description="Text shown to the AI client with this connector (how your vault is organised, conventions to follow). Do not put secrets here")
    max_note_bytes: int = Field(2_000_000, ge=1_000, le=50_000_000, description="Notes larger than this are not read")
    max_base_bytes: int = Field(100_000, ge=1_000, le=5_000_000, description="Bases (.base files) larger than this are not read; they are refused, not cut")
    max_scan_notes: int = Field(5_000, ge=10, le=100_000, description="Most notes scanned for tags, links and search in one call")


    @field_validator("couchdb_url", "database", "username", "vault_name", mode="before")
    @classmethod
    def _trim(cls, v):  # pasted values often carry a stray space or newline
        return v.strip() if isinstance(v, str) else v

    @field_validator("couchdb_url")
    @classmethod
    def _scheme(cls, v: str) -> str:
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return v


def _ts(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ms / 1000)) if ms else "unknown"


def _parse_date(text: str) -> int:
    if not text:
        return 0
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(text.strip(), fmt)) - time.timezone) * 1000
        except ValueError:
            continue
    raise ToolError("Dates look like 2026-11-04 or 2026-11-04T10:00 (UTC).")


class Obsidian(Plugin):
    def __init__(self, ctx, vault: Vault) -> None:
        super().__init__(ctx)
        self.vault = vault
        self.warm: asyncio.Task | None = None
        self.instructions = (ctx.config.instructions or "").strip() or None  # the host passes this to the MCP server

    # ------------------------------------------------------------- plumbing
    async def _ready(self) -> Vault:
        try:
            await self.vault.ensure_ready()
        except CouchError as e:
            raise ToolError(f"The vault database is not available: {e}.") from None
        except LiveSyncError as e:
            raise ToolError(str(e)) from None
        return self.vault

    def _link(self, path: str) -> str:
        name = self.ctx.config.vault_name
        return f"obsidian://open?vault={quote(name, safe='')}&file={quote(path.removesuffix('.md'), safe='')}"

    def _folder(self, folder: str) -> str:
        try:
            f = validate_folder(folder)
        except PathError as e:
            raise ToolError(str(e)) from None
        if f and not self.vault.policy.folder_visible(f):
            raise ToolError("No such folder.")
        return f

    @staticmethod
    def _in_folder(path: str, folder: str) -> bool:
        return not folder or path.casefold().startswith(folder.casefold() + "/")

    def _line(self, m: NoteMeta, extra: str = "") -> str:
        return f"- {_ts(m.mtime)} [{m.path}]({self._link(m.path)})" + (f"\n  {extra}" if extra else "")

    async def _filtered(self, v: Vault, folder: str, tag: str, after: str, name: str = "") -> list[NoteMeta]:
        metas = [m for m in v.visible() if self._in_folder(m.path, folder)]
        since = _parse_date(after)
        if since:
            metas = [m for m in metas if m.mtime > since]
        if name:
            metas = [m for m in metas if name.casefold() in m.path.casefold()]
        if tag:
            wanted = tag.lstrip("#").casefold()
            parsed = await v.parsed(metas)
            metas = [m for m in metas if m.path in parsed and any(t.casefold() == wanted or t.casefold().startswith(wanted + "/") for t in parsed[m.path].tags)]
        return metas

    # ---------------------------------------------------------------- tools
    @tool(tier="read", untrusted=True)
    async def read_note(self, path: str) -> str:
        """Read one note by its vault-relative path, e.g. Claude/Code/demo.md."""
        v = await self._ready()
        try:
            meta, text = await v.read(path)
        except (PathError, LiveSyncError, CouchError) as e:
            raise ToolError(str(e)) from None
        return f"[Open in Obsidian]({self._link(meta.path)})\n\n---\n\n{text}"

    @tool(tier="read", untrusted=True)
    async def search_notes(self, terms: list[str], folder: str = "", tag: str = "", modified_after: str = "", limit: int = 20) -> str:
        """Search note names and contents. A note matches when ANY term occurs (case-insensitive); list synonyms."""
        terms = [t.strip() for t in terms if t and t.strip()][:20]
        if not terms:
            raise ToolError("Give at least one search term.")
        v = await self._ready()
        folder = self._folder(folder)
        metas = (await self._filtered(v, folder, tag, modified_after))[: v.max_scan_notes]
        texts = await v.texts(metas)
        hits: list[tuple[int, NoteMeta, str]] = []
        for m in metas:
            low_path = m.path.casefold()
            text = texts.get(m.path, "")
            low = text.casefold()
            matched = [t for t in terms if t.casefold() in low_path or t.casefold() in low]
            if matched:
                by_path = any(t.casefold() in low_path for t in terms)
                hits.append((len(matched) + (100 if by_path else 0), m, snippet(text, matched[0]) if text else ""))
        hits.sort(key=lambda h: (-h[0], -h[1].mtime))
        limit = max(1, min(limit, 100))
        shown = hits[:limit]
        head = f"{len(hits)} notes match {', '.join(repr(t) for t in terms)} (showing {len(shown)}; narrow with folder, tag or modified_after)."
        return "\n".join([head, *(self._line(m, snip) for _, m, snip in shown)])

    @tool(tier="read")
    async def list_notes(self, folder: str = "", name: str = "", tag: str = "", modified_after: str = "", sort_by: str = "name", limit: int = 100) -> str:
        """List notes, optionally filtered by folder, name fragment, tag or modification date. sort_by is name or modified."""
        v = await self._ready()
        folder = self._folder(folder)
        metas = await self._filtered(v, folder, tag, modified_after, name)
        metas.sort(key=(lambda m: -m.mtime) if sort_by == "modified" else (lambda m: m.path.casefold()))
        limit = max(1, min(limit, 500))
        return "\n".join([f"{len(metas)} notes (showing {min(limit, len(metas))}).", *(self._line(m) for m in metas[:limit])])

    @tool(tier="read")
    async def list_folders(self) -> str:
        """List all folders with the number of notes directly inside each."""
        v = await self._ready()
        counts: Counter[str] = Counter()
        for m in v.visible():
            parts = m.path.split("/")[:-1]
            counts[""] += 0
            for i in range(1, len(parts) + 1):
                counts["/".join(parts[:i])] += 1 if i == len(parts) else 0
        lines = [f"- {f} ({n} notes)" for f, n in sorted(counts.items(), key=lambda kv: kv[0].casefold()) if f and v.policy.folder_visible(f)]
        return "\n".join(lines) or "No folders."

    @tool(tier="read")
    async def list_tags(self) -> str:
        """List all tags, most used first."""
        v = await self._ready()
        parsed = await v.parsed(v.visible())
        counts = Counter(t for p in parsed.values() for t in p.tags)
        return "\n".join(f"- {t} ({n})" for t, n in counts.most_common()) or "No tags."

    @tool(tier="read", untrusted=True)
    async def get_note_metadata(self, path: str) -> str:
        """Frontmatter, tags, outgoing links, backlinks, size and timestamps of one note."""
        v = await self._ready()
        try:
            meta, text = await v.read(path)
        except (PathError, LiveSyncError, CouchError) as e:
            raise ToolError(str(e)) from None
        parsed = (await v.parsed([meta])).get(meta.path)
        visible = v.visible()
        all_parsed = await v.parsed(visible)
        backlinks = sorted(m.path for m in visible if m.path != meta.path and m.path in all_parsed
                           and any(link_target_matches(link, meta.path) for link in all_parsed[m.path].links))
        links = [link for link in (parsed.links if parsed else []) if _link_allowed(v, link)]
        fm = "\n".join(f"  {k}: {val}" for k, val in (parsed.frontmatter if parsed else {}).items()) or "  (none)"
        # Links to .base files: a base that is missing and one the owner blocked both answer "not found".
        targets = base_links(text)[:50]
        base_line = ["bases: " + ", ".join(f"{t} ({'found' if v.base_link_found(t) else 'not found'})" for t in targets)] if targets else []
        return "\n".join([
            f"[Open in Obsidian]({self._link(meta.path)})", f"path: {meta.path}", f"size: {meta.size or len(text.encode())} bytes",
            f"created: {_ts(meta.ctime)}", f"modified: {_ts(meta.mtime)}", f"tags: {', '.join(parsed.tags) if parsed else ''}",
            f"links: {', '.join(links)}", f"backlinks: {', '.join(backlinks)}", *base_line, "frontmatter:", fm,
        ])

    # ---------------------------------------------------------------- bases
    def _base_open_link(self, path: str) -> str:
        """Obsidian's open link needs the extension for anything that is not a note."""
        return f"obsidian://open?vault={quote(self.ctx.config.vault_name, safe='')}&file={quote(path, safe='')}"

    @tool(tier="read", untrusted=True)
    async def list_bases(self, folder: str = "", name: str = "", limit: int = 50, offset: int = 0) -> str:
        """List Obsidian Bases (.base files), newest first: path, size and modified time. folder: a folder; name: text the path must contain."""
        name = str(name or "").strip()
        if len(name) > 100:
            raise ToolError("Search values are limited to 100 characters.")
        v = await self._ready()
        folder = self._folder(folder)
        limit = max(1, min(limit if isinstance(limit, int) and not isinstance(limit, bool) else 50, 200))
        offset = max(0, offset if isinstance(offset, int) and not isinstance(offset, bool) else 0)
        hits = [m for m in v.visible_bases() if self._in_folder(m.path, folder) and (not name or name.casefold() in m.path.casefold())]
        hits.sort(key=lambda m: (-m.mtime, m.path.casefold()))
        page = hits[offset: offset + limit]
        head = f"{len(hits)} bases (showing {offset + 1 if page else 0} to {offset + len(page)})."
        if offset + len(page) < len(hits):
            head += f" Use offset {offset + len(page)} for the next page."
        return "\n".join([head, *(f"- {_ts(m.mtime)} [{m.path}]({self._base_open_link(m.path)}) ({m.size} bytes)" for m in page)])

    @tool(tier="read", untrusted=True)
    async def read_base(self, path: str) -> str:
        """Read one Obsidian Base (.base file) by its vault-relative path: the raw YAML, a summary of its filters, formulas and views, and the notes that embed it. Nothing is evaluated."""
        v = await self._ready()
        try:
            meta, text = await v.read_base(path)
        except (PathError, LiveSyncError, CouchError) as e:
            raise ToolError(str(e)) from None
        result = await asyncio.to_thread(parse_base, text)
        if result.status == "ok":
            summary = ["parse: ok", "summary:", *("  " + line for line in summarise(result.data))]
        else:
            summary = [f"parse: {result.status} ({result.reason})"]
        linked = await v.notes_linking_base(meta.path)
        shown = ", ".join(linked[:50]) + (f" and {len(linked) - 50} more" if len(linked) > 50 else "")
        return "\n".join([
            f"[Open in Obsidian]({self._base_open_link(meta.path)})", f"path: {meta.path}", f"size: {meta.size or len(text.encode())} bytes",
            f"modified: {_ts(meta.mtime)}", *summary, f"linked or embedded from ({len(linked)}): {shown or '(none)'}",
            "Computed by mcpopus from the stored file; nothing in the base was evaluated.", "--- raw ---", text,
        ])

    # --------------------------------------------------------------- writes
    # Off by default twice over: the host policy serves write tools only when the owner turns read-only off and names them,
    # and writable_folders must list a folder. Blocked folders can never be written.
    async def _write(self, coro):
        try:
            return await coro
        except (PathError, WriteRefused, CouchError, LiveSyncError) as e:  # Conflict is a CouchError
            raise ToolError(str(e)[0].upper() + str(e)[1:]) from None

    def _count(self, name: str, amount: float = 1) -> None:
        """Usage counters shown on the plugin's page. Older hosts have no ctx.stats; counting is then skipped."""
        stats = getattr(self.ctx, "stats", None)
        if stats is not None:
            try:
                stats.count(name, amount)
            except Exception:  # noqa: BLE001 - statistics must never break a write
                pass

    @tool(tier="write")
    async def write_note(self, path: str, content: str) -> str:
        """Create a note, or REPLACE the whole note at that path. Use edit_note to change part of a note."""
        v = self.vault
        existed = True
        try:
            v.check_writable(path)
            await v.ensure_ready()
            existed = v._existing(path) is not None
        except (PathError, WriteRefused, CouchError, LiveSyncError) as e:
            raise ToolError(str(e)) from None
        meta = await self._write(v.put_note(path, content))
        self._count("notes_replaced" if existed else "notes_created")
        self._count("bytes_written", len(content.encode()))
        return f"Note {'replaced' if existed else 'created'}: {meta.path}\n[Open in Obsidian]({self._link(meta.path)})"

    @tool(tier="write")
    async def edit_note(self, path: str, content: str, operation: str = "append", old_text: str = "") -> str:
        """Change part of a note. operation: append (default, add to the end), prepend (add after the frontmatter) or replace (swap old_text, which must match exactly once, for content)."""
        v = self.vault
        if operation not in ("append", "prepend", "replace"):
            raise ToolError("operation must be append, prepend or replace.")
        try:
            v.check_writable(path)
            meta, text = await v.read(path)
        except (PathError, WriteRefused, CouchError, LiveSyncError) as e:
            raise ToolError(str(e)) from None
        if operation == "replace":
            if not old_text:
                raise ToolError("old_text is required for replace.")
            first = text.find(old_text)
            if first == -1:
                raise ToolError("old_text was not found in the note.")
            if text.find(old_text, first + 1) != -1:
                raise ToolError("old_text matches more than once. Give a longer, unique string.")
            new = text[:first] + content + text[first + len(old_text):]
        elif operation == "prepend":
            fm = re.match(r"^---\r?\n[\s\S]*?\r?\n---\r?\n", text)
            at = fm.end() if fm else 0
            new = text[:at] + content + "\n" + text[at:]
        else:
            new = text + content if text.endswith("\n") or not text else text + "\n" + content
        saved = await self._write(v.put_note(path, new, base_rev=meta.rev, must_exist=True))
        self._count("notes_edited")
        self._count("bytes_written", len(new.encode()))
        return f"Note edited ({operation}): {saved.path}\n[Open in Obsidian]({self._link(saved.path)})"

    @tool(tier="write")
    async def move_note(self, from_path: str, to_path: str) -> str:
        """Move or rename a note (both paths must be writable). Fails if something already exists at to_path."""
        saved = await self._write(self.vault.move(from_path, to_path))
        self._count("notes_moved")
        return f"Moved: {from_path} -> {saved.path}\n[Open in Obsidian]({self._link(saved.path)})"

    @tool(tier="admin")
    async def delete_note(self, path: str) -> str:
        """Delete a note. LiveSync keeps the entry marked deleted, and every synced device removes the file."""
        v = self.vault
        try:
            v.check_writable(path)
            await v.ensure_ready()
        except (PathError, WriteRefused, CouchError, LiveSyncError) as e:
            raise ToolError(str(e)) from None
        await self._write(v.soft_delete(path))
        self._count("notes_deleted")
        return f"Deleted: {path}"

    @tool(tier="read")
    async def vault_status(self) -> str:
        """How the vault is stored (encryption, obfuscation) and whether it can be read. Counts only, never content."""
        v = self.vault
        try:
            await v.ensure_ready()
            report = await v.diagnose()
        except (CouchError, LiveSyncError) as e:
            return f"The vault database is not available: {e}."
        report.update(notes_in_vault=v.stats.notes_total, notes_readable_here=v.stats.notes_visible,
                      unreadable_entries=v.stats.undecodable, changes_sequence=v.stats.seq,
                      folder_policy={"readable": list(v.policy.readable), "blocked": list(v.policy.blocked)})
        import json

        return json.dumps(report, indent=1)


def _link_allowed(v: Vault, link: str) -> bool:
    """Do not reveal the names of blocked notes through links."""
    target = link.split("#")[0].removesuffix(".md")
    blocked = [m for m in v.notes.values() if not v.policy.allows_path(m.path) and link_target_matches(target, m.path)]
    return not blocked


async def setup(host: PluginHost) -> None:
    ctx = host.context
    cfg = ctx.config
    couch = CouchDB(ctx.http, cfg.couchdb_url, cfg.database, cfg.username, cfg.password.get_secret_value() if cfg.password else "")
    policy = FolderPolicy.build(cfg.readable_folders, cfg.blocked_folders)
    vault = Vault(couch, cfg.passphrase.get_secret_value() if cfg.passphrase else None, policy,
                  max_note_bytes=cfg.max_note_bytes, max_scan_notes=cfg.max_scan_notes,
                  writable=FolderPolicy.build(cfg.writable_folders, []), max_write_bytes=cfg.max_write_bytes, max_base_bytes=cfg.max_base_bytes)
    plugin = Obsidian(ctx, vault)

    async def warm() -> None:  # build the index in the background so the first call is quick; errors surface on use
        try:
            await vault.ensure_ready()
            ctx.logger.info("vault index ready: %d notes", vault.stats.notes_total)
        except Exception as e:
            ctx.logger.warning("vault index not ready yet: %s", type(e).__name__)

    plugin.warm = asyncio.create_task(warm())
    await host.add_plugin(plugin)


async def teardown(host: PluginHost) -> None:
    plugin = next(iter(getattr(host, "_registered", [])), None)
    if plugin is not None and plugin.warm is not None:
        plugin.warm.cancel()

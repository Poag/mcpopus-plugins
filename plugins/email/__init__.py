"""email: read-only mail access over any IMAP server (Gmail, AgentMail, Fastmail and others). Design: docs/email.md."""

from __future__ import annotations

import re

from mcpopus.plugin import BaseModel, Field, Plugin, PluginHost, SecretStr, ToolError, tool
import httpx2
from pydantic import field_validator, model_validator

from .imapbackend import MAX_LIMIT, ImapBackend, MailError, Settings
from .message import Summary, clean, parse_message
from .notify import Notifier, Poller, check_fire_url

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class Config(BaseModel):
    imap_host: str = Field(description="IMAP server host name only, for example imap.gmail.com or imap.fastmail.com (no https://, no port)")
    imap_port: int = Field(993, ge=1, le=65535, description="IMAP over TLS port (993 for most providers)")
    username: str = Field(description="The IMAP login name, usually the full mailbox address")
    password: SecretStr = Field(description="The IMAP password. Gmail and several other providers need an app password rather than the account password")
    ca_certificate: str = Field("", max_length=20_000, description="Extra certificate authority in PEM form, for a private mail server. Leave empty for public providers")
    readable_folders: list[str] = Field([], description="Only these folders (and those below them) are visible, one per line. Empty means all except the blocked ones")
    blocked_folders: list[str] = Field(["Spam", "Junk", "Trash", "Deleted Items", "[Gmail]/Spam", "[Gmail]/Trash"], description="Never visible (nor anything below them), whatever else is set, one per line. Replacing this list removes the defaults")
    max_body_chars: int = Field(20_000, ge=1_000, le=200_000, description="Most characters of message text returned by read_message")
    max_fetch_bytes: int = Field(2_000_000, ge=10_000, le=25_000_000, description="Most bytes downloaded for one message; a larger message is read from its start")
    timeout_seconds: int = Field(20, ge=3, le=120, description="Seconds to wait for the mail server")
    notify_url: str = Field("", max_length=300, description="Optional. The Fire URL of a Claude Code scheduled task with an API trigger (https://api.anthropic.com/v1/claude_code/routines/trig_.../fire). When set, the plugin checks for new mail and tells that task which messages arrived (folder names and ids only). Leave empty for no notifications")
    notify_token: SecretStr = Field(SecretStr(""), description="The API trigger's token for that scheduled task. Required when notify_url is set")
    notify_folders: list[str] = Field([], description="Folders to watch for new mail, one per line. Empty means every visible folder (the readable_folders and blocked_folders settings still apply)")
    poll_seconds: int = Field(300, ge=120, le=3600, description="Seconds between checks for new mail (only used when notify_url is set)")

    @field_validator("imap_host", "username", "ca_certificate", "notify_url", mode="before")
    @classmethod
    def _trim(cls, v):  # pasted values often carry a stray space or newline
        return v.strip() if isinstance(v, str) else v

    @field_validator("imap_host")
    @classmethod
    def _host(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", v):
            raise ValueError("must be a host name only, without https://, a port or a path")
        return v

    @field_validator("username")
    @classmethod
    def _user(cls, v: str) -> str:
        if not v or re.search(r"[\s\"\\\x00-\x1f\x7f]", v):
            raise ValueError("must be the mailbox address, with no spaces or quotes")
        return v

    @field_validator("password")
    @classmethod
    def _pw(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if not raw.strip() or _CTRL.search(raw):
            raise ValueError("must not be empty or contain control characters")
        return v

    @field_validator("notify_url")
    @classmethod
    def _notify_url(cls, v: str) -> str:
        return check_fire_url(v) if v else v

    @field_validator("notify_token", mode="before")
    @classmethod
    def _notify_trim(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("notify_token")
    @classmethod
    def _notify_token(cls, v: SecretStr) -> SecretStr:
        if _CTRL.search(v.get_secret_value()):
            raise ValueError("must not contain control characters")
        return v

    @model_validator(mode="after")
    def _token_needed(self):
        if self.notify_url and not self.notify_token.get_secret_value():
            raise ValueError("notify_token is required when notify_url is set")
        return self

    @field_validator("readable_folders", "blocked_folders", "notify_folders", mode="before")
    @classmethod
    def _lines(cls, v):
        if isinstance(v, list):
            return [x.strip() for x in v if isinstance(x, str) and x.strip()]
        return v


def app_password(host: str, password: str) -> str:
    """Google shows app passwords in groups of four; remove the spaces when the server is Gmail."""
    return re.sub(r"\s+", "", password) if host.lower().endswith(("gmail.com", "googlemail.com")) else password


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n // 1024} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB"


def _line(box: str, m: Summary) -> str:
    unread = " | unread" if m.unread else ""
    return f"- {box}:{m.uid} | {m.date or 'no date'} | {m.sender or 'unknown sender'} | {m.subject or '(no subject)'} | {_size(m.size)}{unread}"


class Email(Plugin):
    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.backend = ImapBackend(self._settings, ctx.logger)
        self.http: httpx2.AsyncClient | None = None
        self.poller: Poller | None = None
        if ctx.config.notify_url:  # no network work here (rule 8): the first check runs from the polling task
            self.http = httpx2.AsyncClient(timeout=httpx2.Timeout(10.0), follow_redirects=False)
            notifier = Notifier(lambda: self.ctx.config.notify_url, lambda: self.ctx.config.notify_token.get_secret_value(), self.http)
            self.poller = Poller(self.backend, notifier, lambda: self.ctx.config.notify_folders, float(ctx.config.poll_seconds), ctx.logger)

    def _settings(self) -> Settings:
        c = self.ctx.config
        password = app_password(c.imap_host, c.password.get_secret_value())  # read at the moment of use
        return Settings(c.imap_host, c.imap_port, c.username, password, c.ca_certificate, float(c.timeout_seconds),
                        tuple(c.readable_folders), tuple(c.blocked_folders), c.max_fetch_bytes)

    @staticmethod
    async def _guard(coro):
        try:
            return await coro
        except MailError as e:
            raise ToolError(str(e)) from None

    @tool(tier="read")
    async def mailbox_status(self) -> str:
        """Check that the mail account can be reached and signed in to. Returns counts only, no message content."""
        r = await self._guard(self.backend.mailbox_report())
        c = self.ctx.config
        text = (f"Signed in to {c.imap_host} as {c.username}. The server listed {r['received']} folder entries; {r['usable']} can be opened, "
                f"{r['visible']} are visible to this connector, {r['hidden']} are hidden by the readable_folders and blocked_folders settings, "
                f"{r['unparsed']} were not understood. The connector can only read: nothing is marked read, moved or deleted.")
        text += " " + self._notify_line()
        if r["visible"] == 0:
            if r["hidden"]:
                text += " Nothing is visible because of the readable_folders or blocked_folders settings: check them (INBOX must be allowed)."
            elif r["unparsed"]:
                text += " The server's folder list is in a form this plugin does not understand: report this."
            else:
                text += " The server reported no folders and INBOX could not be opened: check that the account has a mailbox and that this host and port are its IMAP service."
        return text

    def _notify_line(self) -> str:
        p = self.poller
        if p is None:
            return "New-mail notifications are off (the notify_url setting is empty)."
        return (f"New-mail notifications are on: {p.watching} folders watched every {int(p.interval)} seconds, {p.ticks} checks so far, "
                f"last result: {p.last_result}; {p.pending_count()} waiting to be sent. Nothing is remembered across a reload.")

    @tool(tier="read")
    async def list_folders(self) -> str:
        """List the visible folders (in Gmail every label is a folder). Special-use folders are marked, for example \\Sent or \\All."""
        boxes = await self._guard(self.backend.list_folders())
        lines = []
        for b in boxes:
            special = [a for a in b.attrs if a.startswith("\\") and not a.startswith("\\Has")]
            lines.append(f"- {b.name}" + (f" ({' '.join(special)})" if special else ""))
        return "\n".join(lines) or "No folders are visible."

    @tool(tier="read", untrusted=True)
    async def list_messages(self, folder: str = "INBOX", unread_only: bool = False, limit: int = 20) -> str:
        """List the newest messages in a folder: id (folder:uid), date, sender, subject, size. Reading does not mark anything as read."""
        return await self._search(folder, unread_only=unread_only, limit=limit)

    @tool(tier="read", untrusted=True)
    async def search_messages(self, folder: str = "INBOX", from_address: str = "", to_address: str = "", subject: str = "", text: str = "",
                              since: str = "", before: str = "", unread_only: bool = False, limit: int = 20) -> str:
        """Search one folder. All given criteria must match. Dates look like 2026-11-04. text searches the whole message."""
        return await self._search(folder, from_address=from_address, to_address=to_address, subject=subject, text=text, since=since,
                                  before=before, unread_only=unread_only, limit=limit)

    async def _search(self, folder: str, **kw) -> str:
        name, total, found, validity = await self._guard(self.backend.search(folder, **kw))
        head = f"{total} messages match in {name} (showing {len(found)}, newest first; at most {MAX_LIMIT} per call). UIDVALIDITY {validity}."
        return "\n".join([head, *(_line(name, m) for m in found)])

    @tool(tier="read", untrusted=True)
    async def read_message(self, uid: int, folder: str = "INBOX", max_chars: int = 0) -> str:
        """Read one message by folder and uid (the id shown as INBOX:1234 is folder INBOX, uid 1234). Returns headers, text and an attachment list. Does not mark it as read."""
        cfg = self.ctx.config
        limit = cfg.max_body_chars if max_chars <= 0 else min(max_chars, cfg.max_body_chars)
        name, validity, size, unread, raw, cut = await self._guard(self.backend.fetch(folder, uid))
        p = parse_message(raw, limit, cut)
        h = p.headers
        lines = [f"id: {name}:{uid}", f"uidvalidity: {validity}", f"size: {_size(size)}", f"unread: {'yes' if unread else 'no'}"]
        lines += [f"{k.lower()}: {v}" for k, v in h.items() if v]
        lines.append("attachments: " + ("; ".join(p.attachments) if p.attachments else "none") + (" (not downloaded)" if p.attachments else ""))
        if p.truncated:
            lines.append("note: the message was cut short; raise max_chars (within the max_body_chars setting) or read the rest in the mail client")
        return "\n".join(lines) + "\n\n---\n\n" + (p.body or "(no text)")


async def setup(host: PluginHost) -> None:
    plugin = Email(host.context)
    await host.add_plugin(plugin)
    if plugin.poller is not None:
        plugin.poller.start()


async def teardown(host: PluginHost) -> None:
    plugin = next(iter(getattr(host, "_registered", [])), None)
    if plugin is not None and plugin.poller is not None:
        await plugin.poller.stop()
        await plugin.http.aclose()

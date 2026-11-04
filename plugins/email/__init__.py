"""email: read-only mailbox access over IMAP, built for Gmail with an app password. Design: docs/email.md."""

from __future__ import annotations

import re

from mcpopus.plugin import BaseModel, Field, Plugin, PluginHost, SecretStr, ToolError, tool
from pydantic import field_validator

from .imapbackend import MAX_LIMIT, ImapBackend, MailError, Settings
from .message import Summary, clean, parse_message

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class Config(BaseModel):
    imap_host: str = Field("imap.gmail.com", description="IMAP server host name only, for example imap.gmail.com (no https://, no port)")
    imap_port: int = Field(993, ge=1, le=65535, description="IMAP over TLS port (993 for Gmail)")
    username: str = Field(description="The full mailbox address, for example claude.mail@gmail.com")
    password: SecretStr = Field(description="For Gmail an app password (the account needs 2-step verification). Not the account password")
    ca_certificate: str = Field("", max_length=20_000, description="Extra certificate authority in PEM form, for a private mail server. Leave empty for Gmail")
    readable_mailboxes: list[str] = Field([], description="Only these mailboxes (and those below them) are visible, one per line. Empty means all except the blocked ones")
    blocked_mailboxes: list[str] = Field(["[Gmail]/Spam", "[Gmail]/Trash"], description="Never visible, whatever else is set, one per line. Gmail labels are mailboxes")
    max_body_chars: int = Field(20_000, ge=1_000, le=200_000, description="Most characters of message text returned by read_message")
    max_fetch_bytes: int = Field(2_000_000, ge=10_000, le=25_000_000, description="Most bytes downloaded for one message; a larger message is read from its start")
    timeout_seconds: int = Field(20, ge=3, le=120, description="Seconds to wait for the mail server")

    @field_validator("imap_host", "username", "ca_certificate", mode="before")
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

    @field_validator("readable_mailboxes", "blocked_mailboxes", mode="before")
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

    def _settings(self) -> Settings:
        c = self.ctx.config
        password = app_password(c.imap_host, c.password.get_secret_value())  # read at the moment of use
        return Settings(c.imap_host, c.imap_port, c.username, password, c.ca_certificate, float(c.timeout_seconds),
                        tuple(c.readable_mailboxes), tuple(c.blocked_mailboxes), c.max_fetch_bytes)

    @staticmethod
    async def _guard(coro):
        try:
            return await coro
        except MailError as e:
            raise ToolError(str(e)) from None

    @tool(tier="read")
    async def mailbox_status(self) -> str:
        """Check that the mail account can be reached and signed in to. Returns counts only, no message content."""
        boxes = await self._guard(self.backend.list_mailboxes())
        c = self.ctx.config
        return f"Signed in to {c.imap_host} as {c.username}. {len(boxes)} mailboxes are visible to this connector. The connector can only read: nothing is marked read, moved or deleted."

    @tool(tier="read")
    async def list_mailboxes(self) -> str:
        """List the visible mailboxes. In Gmail every label is a mailbox, and the special ones are marked (\\All, \\Sent, \\Drafts)."""
        boxes = await self._guard(self.backend.list_mailboxes())
        lines = []
        for b in boxes:
            special = [a for a in b.attrs if a.startswith("\\") and not a.startswith("\\Has")]
            lines.append(f"- {b.name}" + (f" ({' '.join(special)})" if special else ""))
        return "\n".join(lines) or "No mailboxes are visible."

    @tool(tier="read", untrusted=True)
    async def list_messages(self, mailbox: str = "INBOX", unread_only: bool = False, limit: int = 20) -> str:
        """List the newest messages in a mailbox: id (mailbox:uid), date, sender, subject, size. Reading does not mark anything as read."""
        return await self._search(mailbox, unread_only=unread_only, limit=limit)

    @tool(tier="read", untrusted=True)
    async def search_messages(self, mailbox: str = "INBOX", from_address: str = "", to_address: str = "", subject: str = "", text: str = "",
                              since: str = "", before: str = "", unread_only: bool = False, limit: int = 20) -> str:
        """Search one mailbox. All given criteria must match. Dates look like 2026-11-04. text searches the whole message."""
        return await self._search(mailbox, from_address=from_address, to_address=to_address, subject=subject, text=text, since=since,
                                  before=before, unread_only=unread_only, limit=limit)

    async def _search(self, mailbox: str, **kw) -> str:
        name, total, found, validity = await self._guard(self.backend.search(mailbox, **kw))
        head = f"{total} messages match in {name} (showing {len(found)}, newest first; at most {MAX_LIMIT} per call). UIDVALIDITY {validity}."
        return "\n".join([head, *(_line(name, m) for m in found)])

    @tool(tier="read", untrusted=True)
    async def read_message(self, uid: int, mailbox: str = "INBOX", max_chars: int = 0) -> str:
        """Read one message by mailbox and uid (the id shown as INBOX:1234 is mailbox INBOX, uid 1234). Returns headers, text and an attachment list. Does not mark it as read."""
        cfg = self.ctx.config
        limit = cfg.max_body_chars if max_chars <= 0 else min(max_chars, cfg.max_body_chars)
        name, validity, size, unread, raw, cut = await self._guard(self.backend.fetch(mailbox, uid))
        p = parse_message(raw, limit, cut)
        h = p.headers
        lines = [f"id: {name}:{uid}", f"uidvalidity: {validity}", f"size: {_size(size)}", f"unread: {'yes' if unread else 'no'}"]
        lines += [f"{k.lower()}: {v}" for k, v in h.items() if v]
        lines.append("attachments: " + ("; ".join(p.attachments) if p.attachments else "none") + (" (not downloaded)" if p.attachments else ""))
        if p.truncated:
            lines.append("note: the message was cut short; raise max_chars (within the max_body_chars setting) or read the rest in the mail client")
        return "\n".join(lines) + "\n\n---\n\n" + (p.body or "(no text)")


async def setup(host: PluginHost) -> None:
    await host.add_plugin(Email(host.context))

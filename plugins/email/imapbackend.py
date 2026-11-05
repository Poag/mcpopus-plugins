"""IMAP access with the standard library. Read-only: mailboxes are opened with EXAMINE and bodies fetched with BODY.PEEK.

imaplib is blocking, so every operation runs in a worker thread with its own short-lived connection. imaplib does not quote
mailbox names or search strings, so this module does. Nothing from a tool argument reaches the wire without validation.
"""

from __future__ import annotations

import asyncio
import datetime
import imaplib
import re
import socket
import ssl
from dataclasses import dataclass

from . import utf7
from .message import Summary

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_BAD = re.compile(r"[\x00-\x1f\x7f]")
_HEADER_FIELDS = "FROM TO SUBJECT DATE"
MAX_LIMIT = 50


class MailError(Exception):
    """A failure whose message is safe to show the model. Never holds a secret or server text."""


def quote(text: str) -> str:
    if _BAD.search(text):
        raise MailError("That value contains control characters.")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_date(text: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text.strip()):
        raise MailError("Dates look like 2026-11-04.")
    try:
        d = datetime.date.fromisoformat(text.strip())
    except ValueError:
        raise MailError("That date does not exist. Dates look like 2026-11-04.") from None
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year:04d}"


@dataclass(frozen=True)
class Mailbox:
    name: str  # as the user sees it (decoded)
    wire: str  # as the server knows it (modified UTF-7)
    attrs: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    username: str
    password: str  # read from the SecretStr at the moment of use by the caller
    ca_certificate: str
    timeout: float
    readable: tuple[str, ...]
    blocked: tuple[str, ...]
    max_fetch_bytes: int


def _under(name: str, folder: str) -> bool:
    n, f = name.casefold(), folder.strip().casefold().rstrip("/")
    return bool(f) and (n == f or n.startswith(f + "/"))


class ImapBackend:
    def __init__(self, get_settings, logger) -> None:
        self._get = get_settings  # called on every operation so the secret is read at the moment of use
        self._log = logger
        self._gate = asyncio.Semaphore(2)

    # ------------------------------------------------------------ plumbing
    def _connect(self, s: Settings) -> imaplib.IMAP4_SSL:
        ctx = ssl.create_default_context()
        if s.ca_certificate:
            try:
                ctx.load_verify_locations(cadata=s.ca_certificate)
            except (ssl.SSLError, ValueError):
                raise MailError("The ca_certificate setting is not a valid PEM certificate.") from None
        try:
            conn = imaplib.IMAP4_SSL(s.host, s.port, ssl_context=ctx, timeout=s.timeout)
        except socket.gaierror:
            raise MailError("Cannot find the mail server. Check the imap_host setting.") from None
        except ssl.SSLCertVerificationError:
            raise MailError("The mail server's certificate was not accepted. Check imap_host, or set ca_certificate for a private server.") from None
        except ssl.SSLError:
            raise MailError("The TLS handshake with the mail server failed. Check imap_host and imap_port (993 for most providers).") from None
        except (TimeoutError, OSError):
            raise MailError("Cannot reach the mail server. Check imap_host and imap_port, and that the host can make outbound connections.") from None
        except (imaplib.IMAP4.error, EOFError):
            raise MailError("The mail server did not answer like an IMAP server. Check imap_host and imap_port.") from None
        conn._encoding = "utf-8"  # criteria are sent as UTF-8 after CHARSET UTF-8
        try:
            conn.login(s.username, s.password)
        except imaplib.IMAP4.error:
            self._close(conn)
            raise MailError("The mail server refused the sign-in. Check the username and password settings. Some providers (Gmail among them) need an app password rather than the account password.") from None
        except (UnicodeError, ValueError):
            self._close(conn)
            raise MailError("The password setting holds characters the login cannot send. Check it.") from None
        except (OSError, EOFError, imaplib.IMAP4.abort):
            self._close(conn)
            raise MailError("The mail server closed the connection during sign-in. Try again.") from None
        return conn

    @staticmethod
    def _close(conn) -> None:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - closing is best effort
            try:
                conn.shutdown()
            except Exception:  # noqa: BLE001
                pass

    async def _run(self, fn):
        s: Settings = self._get()

        def work():
            conn = self._connect(s)
            try:
                return fn(conn, s)
            except MailError:
                raise
            except (imaplib.IMAP4.abort, EOFError, TimeoutError, OSError):
                raise MailError("The connection to the mail server was lost. Try again.") from None
            except imaplib.IMAP4.error:
                raise MailError("The mail server rejected the request.") from None
            finally:
                self._close(conn)

        async with self._gate:
            try:
                return await asyncio.to_thread(work)
            except MailError:
                raise
            except Exception as e:  # noqa: BLE001 - logged by class only; the host shows a generic message
                self._log.warning("mail operation failed: %s", type(e).__name__)
                raise

    # ----------------------------------------------------------- mailboxes
    @staticmethod
    def _visible(s: Settings, name: str) -> bool:
        if name.upper() == "INBOX":
            name = "INBOX"
        if any(_under(name, b) for b in s.blocked):
            return False
        return not s.readable or any(_under(name, r) for r in s.readable)

    _LIST_LINE = re.compile(r'\((?P<attrs>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$', re.I)

    def _scan(self, conn) -> tuple[list[Mailbox], int, int]:
        """Ask the server for every mailbox. Returns (selectable mailboxes, lines received, lines not understood)."""
        found: dict[str, Mailbox] = {}
        received = unparsed = 0
        for pattern in ("*", "%"):  # some servers answer only one of the two
            typ, data = conn.list('""', pattern)
            if typ != "OK":
                continue
            for item in data or []:
                if item is None:
                    continue
                if isinstance(item, tuple):  # the name arrived as a literal
                    text = item[0].decode("utf-8", "replace") + '"' + item[1].decode("utf-8", "replace").replace('"', "") + '"'
                else:
                    text = item.decode("utf-8", "replace")
                if not text.strip():
                    continue
                received += 1
                m = self._LIST_LINE.match(text.strip())
                if not m:
                    unparsed += 1
                    continue
                raw = m.group("name").strip()
                if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
                    raw = re.sub(r"\\(.)", r"\1", raw[1:-1])
                attrs = tuple(a for a in m.group("attrs").split() if a)
                if "\\noselect" in {a.casefold() for a in attrs}:
                    continue
                found.setdefault(raw, Mailbox(utf7.decode(raw), raw, attrs))
            if found:
                break
        if not any(b.name.upper() == "INBOX" for b in found.values()):
            # Every IMAP server has an INBOX. If it is missing from the list, try opening it.
            try:
                typ, _ = conn.select('"INBOX"', readonly=True)
            except (imaplib.IMAP4.error, OSError):
                typ = "NO"
            if typ == "OK":
                found["INBOX"] = Mailbox("INBOX", "INBOX", ())
        return list(found.values()), received, unparsed

    def _list(self, conn, s: Settings) -> list[Mailbox]:
        return [b for b in self._scan(conn)[0] if self._visible(s, b.name)]

    async def list_mailboxes(self) -> list[Mailbox]:
        return await self._run(self._list)

    async def mailbox_report(self) -> dict[str, int]:
        """Counts only: what the server listed and how many the settings hide. Used by mailbox_status."""

        def work(conn, s: Settings):
            boxes, received, unparsed = self._scan(conn)
            visible = [b for b in boxes if self._visible(s, b.name)]
            return {"received": received, "unparsed": unparsed, "usable": len(boxes), "visible": len(visible), "hidden": len(boxes) - len(visible)}

        return await self._run(work)

    @staticmethod
    def _resolve(boxes: list[Mailbox], wanted: str) -> Mailbox:
        wanted = wanted.strip() or "INBOX"
        if len(wanted) > 200 or _BAD.search(wanted):
            raise MailError("No such mailbox.")
        for b in boxes:
            if b.name == wanted:
                return b
        for b in boxes:  # case-insensitive fallback, only when it is unambiguous
            if b.name.casefold() == wanted.casefold():
                return b
        raise MailError("No such mailbox.")

    def _open(self, conn, s: Settings, mailbox: str) -> tuple[Mailbox, int]:
        box = self._resolve(self._list(conn, s), mailbox)
        typ, _ = conn.select(quote(box.wire), readonly=True)  # EXAMINE
        if typ != "OK":
            raise MailError("No such mailbox.")
        validity = 0
        try:
            validity = int((conn.response("UIDVALIDITY")[1] or [b"0"])[0])
        except (ValueError, TypeError, IndexError):
            pass
        return box, validity

    # ------------------------------------------------------------ messages
    @staticmethod
    def _entries(data) -> list[dict]:
        entries: list[dict] = []
        for item in data or []:
            if item is None:
                continue
            if isinstance(item, tuple):
                entries.append({"meta": item[0], "lit": item[1]})
            elif re.match(rb"^\s*\d+ \(", item):
                entries.append({"meta": item, "lit": None})
            elif entries:  # attributes that follow a literal
                entries[-1]["meta"] += b" " + item
        return entries

    @staticmethod
    def _attrs(meta: bytes) -> tuple[int, bool, int]:
        text = meta.decode("ascii", "replace")
        uid = re.search(r"\bUID (\d+)", text)
        size = re.search(r"\bRFC822\.SIZE (\d+)", text)
        flags = re.search(r"\bFLAGS \(([^)]*)\)", text)
        seen = bool(flags and "\\seen" in flags.group(1).casefold())
        return (int(uid.group(1)) if uid else 0), not seen, (int(size.group(1)) if size else 0)

    def _summaries(self, conn, uids: list[int]) -> list[Summary]:
        if not uids:
            return []
        from .message import parse_headers

        typ, data = conn.uid("FETCH", ",".join(str(u) for u in uids), f"(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})])")
        if typ != "OK":
            raise MailError("The mail server would not return the message list.")
        out: dict[int, Summary] = {}
        for e in self._entries(data):
            uid, unread, size = self._attrs(e["meta"])
            if not uid or uid not in uids:
                continue
            h = parse_headers(e["lit"] or b"")
            out[uid] = Summary(uid, h.get("Date", ""), h.get("From", ""), h.get("To", ""), h.get("Subject", ""), size, unread)
        return [out[u] for u in uids if u in out]

    async def search(self, mailbox: str, *, unread_only: bool = False, from_address: str = "", to_address: str = "", subject: str = "",
                     text: str = "", since: str = "", before: str = "", limit: int = 20) -> tuple[str, int, list[Summary], int]:
        """Return (mailbox name, matches, newest-first summaries, uidvalidity)."""
        limit = max(1, min(int(limit), MAX_LIMIT))
        crit: list[str] = []
        for key, value in (("FROM", from_address), ("TO", to_address), ("SUBJECT", subject), ("TEXT", text)):
            value = value.strip()
            if value:
                if len(value) > 200:
                    raise MailError(f"The {key.lower()} search text is too long (200 characters at most).")
                crit += [key, quote(value)]
        if since.strip():
            crit += ["SINCE", imap_date(since)]
        if before.strip():
            crit += ["BEFORE", imap_date(before)]
        if unread_only:
            crit.append("UNSEEN")
        if not crit:
            crit = ["ALL"]

        def work(conn, s: Settings):
            box, validity = self._open(conn, s, mailbox)
            typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *[c.encode("utf-8") for c in crit])
            if typ != "OK":
                raise MailError("The mail server would not run that search.")
            uids = sorted({int(x) for x in (data[0] or b"").split() if x.isdigit()}, reverse=True)
            return box.name, len(uids), self._summaries(conn, uids[:limit]), validity

        return await self._run(work)

    async def fetch(self, mailbox: str, uid: int) -> tuple[str, int, int, bool, bytes, bool]:
        """Return (mailbox name, uidvalidity, size, unread, raw bytes, truncated)."""
        if not isinstance(uid, int) or isinstance(uid, bool) or not 1 <= uid <= 4_294_967_295:
            raise MailError("uid must be a number from the message id, such as the 1234 in INBOX:1234.")

        def work(conn, s: Settings):
            box, validity = self._open(conn, s, mailbox)
            typ, data = conn.uid("FETCH", str(uid), f"(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.{s.max_fetch_bytes}>)")
            if typ != "OK":
                raise MailError("The mail server would not return that message.")
            for e in self._entries(data):
                got, unread, size = self._attrs(e["meta"])
                if got == uid and e["lit"] is not None:
                    return box.name, validity, size, unread, e["lit"], size > len(e["lit"])
            raise MailError("No such message.")

        return await self._run(work)

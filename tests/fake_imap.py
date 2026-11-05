"""A small fake IMAP server over TLS for the email plugin tests. It records every command so tests can prove the plugin only reads."""

from __future__ import annotations

import asyncio
import datetime
import email
import email.policy
import re
import ssl
import tempfile
from email.utils import parsedate_to_datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ALLOWED = {"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "UID", "LOGOUT", "NOOP"}  # anything else is a write or unknown
MONTHS = {m: i + 1 for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}


def make_cert(host: str = "localhost") -> tuple[str, bytes, str]:
    """Return (cert PEM text, key PEM bytes, directory) for a self-signed certificate valid for 127.0.0.1 and localhost."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host), x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    kpem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem.decode(), kpem, tempfile.mkdtemp()


def tokens(line: str) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == " ":
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < len(line) and line[j] != '"':
                if line[j] == "\\" and j + 1 < len(line):
                    j += 1
                buf.append(line[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < len(line) and line[j] != " ":
                j += 1
            out.append(line[i:j])
            i = j
    return out


class Msg:
    def __init__(self, uid: int, raw: bytes, flags: tuple[str, ...] = ()) -> None:
        self.uid, self.raw, self.flags = uid, raw, flags
        self.parsed = email.message_from_bytes(raw, policy=email.policy.default)

    def date(self) -> datetime.date:
        try:
            return parsedate_to_datetime(self.parsed["Date"]).date()
        except Exception:  # noqa: BLE001
            return datetime.date(1970, 1, 1)


class FakeImap:
    def __init__(self, user: str = "claude.mail@gmail.com", password: str = "abcdefghijklmnop") -> None:
        self.user, self.password = user, password
        self.boxes: dict[str, dict] = {}  # wire name -> {"attrs": str, "msgs": [Msg]}
        self.commands: list[str] = []
        self.logins = 0
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self.cert_pem, self._key_pem, d = make_cert()
        self._dir = Path(d)
        (self._dir / "c.pem").write_text(self.cert_pem)
        (self._dir / "k.pem").write_bytes(self._key_pem)
        self.list_style = "normal"  # normal | atoms | empty | no_inbox | percent_only
        self.fail_with: str | None = None  # "drop" closes the connection after login

    def add(self, box: str, attrs: str = "", msgs: list[Msg] | None = None) -> None:
        self.boxes[box] = {"attrs": attrs, "msgs": msgs or []}

    async def start(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self._dir / "c.pem", self._dir / "k.pem")
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0, ssl=ctx)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------------ protocol
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def send(*lines: bytes | str) -> None:
            for line in lines:
                writer.write(line if isinstance(line, bytes) else line.encode())

        state = {"auth": False, "box": None}
        try:
            send("* OK [CAPABILITY IMAP4rev1] fake ready\r\n")
            await writer.drain()
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                tag, _, rest = line.partition(" ")
                cmd, _, args = rest.partition(" ")
                self.commands.append(f"{cmd.upper()} {args.split(' ')[0].upper() if cmd.upper() == 'UID' else ''}".strip())
                up = cmd.upper()
                if up not in ALLOWED:
                    send(f"{tag} BAD command not allowed in this fake\r\n")
                elif up == "CAPABILITY":
                    send("* CAPABILITY IMAP4rev1\r\n", f"{tag} OK done\r\n")
                elif up == "NOOP":
                    send(f"{tag} OK\r\n")
                elif up == "LOGOUT":
                    send("* BYE bye\r\n", f"{tag} OK logout\r\n")
                    await writer.drain()
                    break
                elif up == "LOGIN":
                    t = tokens(args)
                    self.logins += 1
                    if len(t) == 2 and t[0] == self.user and t[1] == self.password:
                        state["auth"] = True
                        send(f"{tag} OK [CAPABILITY IMAP4rev1] logged in\r\n")
                        if self.fail_with == "drop":
                            await writer.drain()
                            break
                    else:
                        send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)\r\n")
                elif not state["auth"]:
                    send(f"{tag} BAD not signed in\r\n")
                elif up == "LIST":
                    pattern = tokens(args)[-1]
                    style = self.list_style
                    if style == "empty" or (style == "percent_only" and pattern == "*"):
                        send(f"{tag} OK listed\r\n")
                    else:
                        for name, b in self.boxes.items():
                            if style == "no_inbox" and name == "INBOX":
                                continue
                            if style == "atoms" and " " not in name and '"' not in name:
                                send(f'* LIST ({b["attrs"]}) NIL {name}\r\n')
                                continue
                            enc = name.replace("\\", "\\\\").replace('"', '\\"')
                            send(f'* LIST ({b["attrs"]}) "/" "{enc}"\r\n')
                        send(f"{tag} OK listed\r\n")
                elif up == "EXAMINE":
                    name = tokens(args)[0]
                    if name not in self.boxes:
                        send(f"{tag} NO [NONEXISTENT] Unknown Mailbox\r\n")
                    else:
                        state["box"] = name
                        send(f"* {len(self.boxes[name]['msgs'])} EXISTS\r\n", "* OK [UIDVALIDITY 777] ok\r\n", f"{tag} OK [READ-ONLY] examined\r\n")
                elif up == "UID":
                    self._uid(tag, args, state, send)
                else:
                    send(f"{tag} BAD unknown\r\n")
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    def _match(self, m: Msg, crit: list[str]) -> bool:
        i = 0
        while i < len(crit):
            k = crit[i].upper()
            if k == "ALL":
                i += 1
            elif k == "UNSEEN":
                if "\\Seen" in m.flags:
                    return False
                i += 1
            elif k in ("FROM", "TO", "SUBJECT"):
                if crit[i + 1].casefold() not in str(m.parsed[k.title()] or "").casefold():
                    return False
                i += 2
            elif k == "TEXT":
                if crit[i + 1].casefold() not in m.raw.decode("utf-8", "replace").casefold():
                    return False
                i += 2
            elif k in ("SINCE", "BEFORE"):
                d, mo, y = crit[i + 1].split("-")
                want = datetime.date(int(y), MONTHS[mo], int(d))
                if (k == "SINCE" and m.date() < want) or (k == "BEFORE" and m.date() >= want):
                    return False
                i += 2
            else:
                raise ValueError(k)
        return True

    def _uid(self, tag: str, args: str, state: dict, send) -> None:
        sub, _, rest = args.partition(" ")
        box = self.boxes.get(state["box"] or "")
        if box is None:
            send(f"{tag} BAD no mailbox selected\r\n")
            return
        if sub.upper() == "SEARCH":
            t = tokens(rest)
            if t[:2] == ["CHARSET", "UTF-8"]:
                t = t[2:]
            try:
                hits = [str(m.uid) for m in box["msgs"] if self._match(m, t)]
            except ValueError:
                send(f"{tag} BAD unsupported criterion\r\n")
                return
            send("* SEARCH" + "".join(" " + h for h in hits) + "\r\n", f"{tag} OK searched\r\n")
        elif sub.upper() == "FETCH":
            m_ = re.match(r"([\d,]+) \((.*)\)$", rest)
            wanted = {int(x) for x in m_.group(1).split(",")}
            items = m_.group(2)
            part = re.search(r"BODY\.PEEK\[(HEADER\.FIELDS \(([^)]*)\))?\](<0\.(\d+)>)?", items)
            if "BODY[" in items and "PEEK" not in items:
                send(f"{tag} BAD fake refuses non-PEEK body fetches\r\n")
                return
            for m in box["msgs"]:
                if m.uid not in wanted:
                    continue
                head = f"UID {m.uid} RFC822.SIZE {len(m.raw)} FLAGS ({' '.join(m.flags)})"
                if part:
                    if part.group(1):
                        names = part.group(2).split()
                        lit = "".join(f"{n.title()}: {m.parsed[n.title()]}\r\n" for n in names if m.parsed[n.title()]).encode() + b"\r\n"
                        label = f"BODY[HEADER.FIELDS ({part.group(2)})]"
                    else:
                        lit = m.raw[: int(part.group(4))] if part.group(4) else m.raw
                        label = "BODY[]<0>" if part.group(4) else "BODY[]"
                    send(f"* {m.uid} FETCH ({head} {label} {{{len(lit)}}}\r\n", lit, ")\r\n")
                else:
                    send(f"* {m.uid} FETCH ({head})\r\n")
            send(f"{tag} OK fetched\r\n")
        else:
            send(f"{tag} BAD unsupported\r\n")

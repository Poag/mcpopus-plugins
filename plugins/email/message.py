"""Turn raw RFC 5322 messages into bounded, plain text for the model. Hostile input must not crash this."""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from html.parser import HTMLParser

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BIDI = re.compile("[‪-‮⁦-⁩​-‏﻿]")
_SKIP = {"script", "style", "head", "title", "template", "noscript"}
_BREAK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "hr", "ul", "ol"}


def clean(text: str, limit: int = 500) -> str:
    """One line, no control or direction-changing characters, bounded."""
    text = re.sub(r"\s+", " ", _BIDI.sub("", _CTRL.sub(" ", text))).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def clean_body(text: str) -> str:
    text = _BIDI.sub("", _CTRL.sub("", text.replace("\r\n", "\n").replace("\r", "\n")))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.href: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        if tag in _BREAK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self.href.append(href if href.lower().startswith(("http://", "https://", "mailto:")) else None)

    def handle_endtag(self, tag):
        if tag in _SKIP and self.skip:
            self.skip -= 1
        if tag in _BREAK:
            self.parts.append("\n")
        if tag == "a" and self.href:
            href = self.href.pop()
            if href and not self.skip:
                self.parts.append(f" ({href[:300]})")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep what was read
        pass
    text = "".join(p.parts)
    text = re.sub(r"[ \t\f\v ]+", " ", text)
    return re.sub(r" ?\n ?", "\n", text)


@dataclass
class Summary:
    uid: int
    date: str = ""
    sender: str = ""
    to: str = ""
    subject: str = ""
    size: int = 0
    unread: bool = False


@dataclass
class Parsed:
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    attachments: list[str] = field(default_factory=list)
    truncated: bool = False


def _hdr(msg: EmailMessage, name: str, limit: int = 500) -> str:
    try:
        return clean(str(msg.get(name, "") or ""), limit)
    except Exception:  # noqa: BLE001 - undecodable header
        return ""


def parse_headers(raw: bytes) -> dict[str, str]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return {k: _hdr(msg, k) for k in ("From", "To", "Cc", "Subject", "Date")}


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB"


def parse_message(raw: bytes, max_chars: int, truncated: bool = False) -> Parsed:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    headers = {k: _hdr(msg, k) for k in ("From", "To", "Cc", "Reply-To", "Subject", "Date", "Message-ID")}
    attachments: list[str] = []
    plain: list[str] = []
    html: list[str] = []
    count = 0
    for part in msg.walk():
        count += 1
        if count > 200:  # a message built to be a maze
            truncated = True
            break
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        name = clean(part.get_filename() or "", 120)
        if disp == "attachment" or (name and ctype not in ("text/plain", "text/html")) or not ctype.startswith("text/"):
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:  # noqa: BLE001
                payload = b""
            attachments.append(f"{name or '(unnamed)'} ({ctype}, {_size(len(payload))})")
            continue
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001 - unknown charset and similar
            try:
                content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(content, str):
            continue
        if disp == "attachment":
            continue
        (html if ctype == "text/html" else plain).append(content)
    text = "\n\n".join(plain) if plain else html_to_text("\n".join(html))
    text = clean_body(text)
    if len(text) > max_chars:
        text, truncated = text[:max_chars], True
    return Parsed(headers, text, attachments[:50], truncated)

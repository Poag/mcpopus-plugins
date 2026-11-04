"""IMAP modified UTF-7 (RFC 3501 section 5.1.3) for mailbox names."""

from __future__ import annotations

import base64
import re

_ENC = re.compile(r"[^\x20-\x7e]+")


def encode(name: str) -> str:
    out: list[str] = []
    last = 0
    for m in _ENC.finditer(name):
        out.append(name[last:m.start()].replace("&", "&-"))
        b64 = base64.b64encode(m.group().encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
        out.append(f"&{b64}-")
        last = m.end()
    out.append(name[last:].replace("&", "&-"))
    return "".join(out)


def decode(name: str) -> str:
    def repl(m: re.Match) -> str:
        body = m.group(1)
        if not body:
            return "&"
        raw = body.replace(",", "/")
        raw += "=" * (-len(raw) % 4)
        try:
            return base64.b64decode(raw).decode("utf-16-be")
        except Exception:  # noqa: BLE001 - a malformed name is shown as received
            return m.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, name)

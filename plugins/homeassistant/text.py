"""Turn Home Assistant data into bounded plain text. Names, attributes and messages are set by people, devices and integrations, so
they are data: control characters and direction overrides are removed and every value is size-capped."""

from __future__ import annotations

import json
import re
from typing import Any

from .policy import ENTITY_RE, EntityPolicy

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BIDI = re.compile("[‪-‮⁦-⁩​-‏﻿]")
MAX_ATTRS = 40
ATTR_VALUE_CHARS = 300
ATTRS_TOTAL_CHARS = 3000


def clean(value: Any, limit: int = 200) -> str:
    """One line, no control or direction-changing characters, bounded."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    text = re.sub(r"\s+", " ", _BIDI.sub("", _CTRL.sub(" ", text))).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def scrub(value: Any, policy: EntityPolicy, depth: int = 0) -> Any:
    """Remove references to hidden entities from an attribute value (lists of entity ids, such as a group's members)."""
    if isinstance(value, str):
        return None if ENTITY_RE.fullmatch(value.lower()) and not policy.visible(value) else value
    if isinstance(value, (list, tuple)) and depth < 3:
        return [s for s in (scrub(v, policy, depth + 1) for v in value[:200]) if s is not None]
    if isinstance(value, dict) and depth < 3:
        return {str(k): s for k, v in list(value.items())[:100] if (s := scrub(v, policy, depth + 1)) is not None}
    return value


def attributes(attrs: Any, policy: EntityPolicy, redact: frozenset[str]) -> list[str]:
    if not isinstance(attrs, dict):
        return []
    lines: list[str] = []
    used = 0
    for key in sorted(attrs, key=str)[:MAX_ATTRS]:
        name = clean(str(key), 60)
        if str(key).casefold() in redact:
            line = f"  {name}: [redacted]"
        else:
            value = scrub(attrs[key], policy)
            if value is None:
                continue
            line = f"  {name}: {clean(value, ATTR_VALUE_CHARS)}"
        used += len(line)
        if used > ATTRS_TOTAL_CHARS:
            lines.append("  … (more attributes not shown)")
            break
        lines.append(line)
    if len(attrs) > MAX_ATTRS:
        lines.append(f"  … ({len(attrs) - MAX_ATTRS} more attributes not shown)")
    return lines

"""Which entities and services the plugin may touch. Enforced here, server side; tool descriptions are not a control.

A denied entity must look exactly like a missing one, so callers use `visible()` before anything is fetched, listed or counted.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

ENTITY_RE = re.compile(r"^[a-z0-9_]{1,64}\.[a-z0-9_]{1,128}$")
SERVICE_RE = re.compile(r"^[a-z0-9_]{1,64}\.[a-z0-9_]{1,128}$")
GLOB_RE = re.compile(r"^[a-z0-9_.*?\[\]!-]{1,128}$")
_TOKEN = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")


def norm_glob(pattern: str) -> str:
    p = pattern.strip().lower()
    if not GLOB_RE.fullmatch(p):
        raise ValueError("patterns use lower-case letters, digits, '_', '.', '*', '?' and [] only (at most 128 characters)")
    return p


def domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


@dataclass(frozen=True)
class EntityPolicy:
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    deny_domains: frozenset[str]

    @classmethod
    def build(cls, allow: list[str], deny: list[str], deny_domains: list[str]) -> EntityPolicy:
        return cls(tuple(allow), tuple(deny), frozenset(d.strip().lower() for d in deny_domains if d.strip()))

    def visible(self, entity_id: str) -> bool:
        """True when the entity may be shown. The deny rules always win over the allow list."""
        e = entity_id.lower()
        if not ENTITY_RE.fullmatch(e):
            return False
        if domain_of(e) in self.deny_domains:
            return False
        if any(fnmatch.fnmatchcase(e, p) for p in self.deny):
            return False
        return not self.allow or any(fnmatch.fnmatchcase(e, p) for p in self.allow)

    def hidden_by(self, entity_id: str) -> str | None:
        """Which rule hides this id: "domain", "deny" or "allow" (checked in that order), or None when it is visible.

        The answer depends on the id's text and the settings only, never on whether Home Assistant has the entity, so naming the rule
        reveals nothing about what exists. Used only for owner-facing messages of the definition tools."""
        e = entity_id.lower()
        if domain_of(e) in self.deny_domains:
            return "domain"
        if any(fnmatch.fnmatchcase(e, p) for p in self.deny):
            return "deny"
        if self.allow and not any(fnmatch.fnmatchcase(e, p) for p in self.allow):
            return "allow"
        return None

    def service_allowed(self, service: str, allowed: tuple[str, ...]) -> bool:
        return service in allowed and domain_of(service) not in self.deny_domains

    def mentions_hidden(self, text: str, known_domains: frozenset[str]) -> bool:
        """True when free text names something shaped like an entity id that the policy hides.

        Only domains Home Assistant really has count, so version numbers and file names are not mistaken for entity ids."""
        return any(domain_of(t) in known_domains and not self.visible(t) for t in _TOKEN.findall(text.lower()) if ENTITY_RE.fullmatch(t))


TARGET_KEYS = frozenset({"entity_id", "area_id", "device_id", "label_id", "floor_id", "target"})
DATA_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_DATA_KEYS = 20
MAX_DATA_CHARS = 2000


def writable(policy: EntityPolicy, write_allow: tuple[str, ...], entity_id: str) -> bool:
    """A write needs the entity to be visible AND to match the write allow list. An empty write list allows nothing."""
    e = entity_id.lower()
    return policy.visible(e) and any(fnmatch.fnmatchcase(e, p) for p in write_allow)


def _strings(value, depth=0):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)) and depth < 4:
        for v in value:
            yield from _strings(v, depth + 1)
    elif isinstance(value, dict) and depth < 4:
        for v in value.values():
            yield from _strings(v, depth + 1)


def check_service_data(data, target: str) -> str | None:
    """Why the extra service data is refused, or None. The target is named separately and can never be widened by the data."""
    import json

    if not isinstance(data, dict):
        return "data must be an object of service fields, for example {\"brightness\": 120}."
    if len(data) > MAX_DATA_KEYS:
        return f"data may have at most {MAX_DATA_KEYS} fields."
    for key in data:
        if not isinstance(key, str) or not DATA_KEY_RE.fullmatch(key):
            return "data field names use lower-case letters, digits and underscores only."
        if key in TARGET_KEYS:
            return "data may not name targets (entity_id, area_id, device_id, label_id, floor_id or target). Use the entity_id argument for the one entity to change."
    try:
        text = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return "data must be plain JSON values."
    if len(text) > MAX_DATA_CHARS:
        return f"data is too large ({MAX_DATA_CHARS} characters at most)."
    for s in _strings(data):
        if ENTITY_RE.fullmatch(s.strip().lower()) and s.strip().lower() != target:
            return "data may not refer to other entities."
    return None

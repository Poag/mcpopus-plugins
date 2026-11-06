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

    def service_allowed(self, service: str, allowed: tuple[str, ...]) -> bool:
        return service in allowed and domain_of(service) not in self.deny_domains

    def mentions_hidden(self, text: str, known_domains: frozenset[str]) -> bool:
        """True when free text names something shaped like an entity id that the policy hides.

        Only domains Home Assistant really has count, so version numbers and file names are not mistaken for entity ids."""
        return any(domain_of(t) in known_domains and not self.visible(t) for t in _TOKEN.findall(text.lower()) if ENTITY_RE.fullmatch(t))

"""homeassistant: read-only view of a Home Assistant home, with entity and domain policy enforced by the plugin.
Design and verified facts: docs/DESIGN-homeassistant.md."""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from collections import Counter, deque
from datetime import UTC, datetime, timedelta

import httpx2
from mcpopus.plugin import BaseModel, Field, Plugin, PluginHost, SecretStr, ToolError, tool
from pydantic import field_validator, model_validator

from . import text as tx
from .automations import ID_BODY, Context, DefinitionError, build_config, definition_problem
from .client import HAError, HomeAssistant, RegistryUnavailable, Settings, check_base_url, ssl_context
from .policy import ENTITY_RE, SERVICE_RE, EntityPolicy, check_service_data, domain_of, norm_glob, writable

DEFAULT_DENIED_DOMAINS = ["lock", "alarm_control_panel", "camera", "person", "device_tracker", "notify"]
DEFAULT_REDACTED = ["access_token", "entity_picture", "latitude", "longitude", "gps_accuracy", "ip_address", "mac", "mac_address", "token", "password"]
HISTORY_POINTS = 200
LOGBOOK_ENTRIES = 100
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class Config(BaseModel):
    base_url: str = Field(description="Home Assistant address, for example http://homeassistant.local:8123 or https://ha.example.com (no path, no credentials)")
    token: SecretStr = Field(description="A long-lived access token. Create it on a dedicated Home Assistant user's profile page, and revoke it there to cut access. The automation definition tools only work if that user is an administrator; without them a non-administrator is safer")
    verify_tls: bool = Field(True, description="Check the server certificate for https. Turn off only on a trusted network, and prefer ca_certificate")
    ca_certificate: str = Field("", max_length=20_000, description="Extra certificate authority in PEM form, for a private certificate. Leave empty for a public certificate")
    entity_allowlist: list[str] = Field([], description="Only entities matching one of these patterns are visible (one per line, for example light.* or sensor.kitchen_*). Empty means all except the denied ones")
    entity_denylist: list[str] = Field([], description="Entities matching these patterns are never visible, whatever the allow list says (one per line)")
    domain_denylist: list[str] = Field(DEFAULT_DENIED_DOMAINS, description="Whole domains that are never visible (one per line). Replacing this list removes the defaults. Suggested defaults, for you to change")
    allowed_services: list[str] = Field([], description="domain.service pairs that call_service may use, one per line, for example light.turn_on. Empty means no service may be called")
    write_entity_allowlist: list[str] = Field([], description="Entities call_service may act on (one pattern per line, for example light.*). Empty means none: this is separate from what can be read, and an entity must also be visible. Needs the call_service tool ticked under Policy in the admin UI")
    automation_categories: list[str] = Field([], description="Automations in these Home Assistant categories (names, one per line) may be triggered, enabled and disabled by the automation tools. Empty means none, except automations matching write_entity_allowlist")
    automation_id_prefix: str = Field("claude_", description="Automations this connector creates, edits or deletes must have a config id starting with this. Human-written automations never do, so they cannot be changed or deleted")
    max_writes_per_minute: int = Field(10, ge=1, le=120, description="Most service calls per minute, on top of the host's own limit")
    max_results: int = Field(50, ge=1, le=500, description="Most entities or entries returned by one call")
    max_history_hours: int = Field(72, ge=1, le=720, description="Longest window for history and logbook queries")
    redact_attributes: list[str] = Field(DEFAULT_REDACTED, description="Attribute names whose values are never shown (one per line, case-insensitive). Replacing this list removes the defaults")
    instructions: str = Field("", max_length=4000, description="Text shown to the AI client with this connector (how your home is organised). Do not put secrets here")
    timeout_seconds: int = Field(15, ge=3, le=120, description="Seconds to wait for Home Assistant")
    max_response_bytes: int = Field(12_000_000, ge=100_000, le=100_000_000, description="Largest reply accepted from Home Assistant (the full entity list is the biggest)")

    @field_validator("base_url", "ca_certificate", mode="before")
    @classmethod
    def _trim(cls, v):  # pasted values often carry a stray space or newline
        return v.strip() if isinstance(v, str) else v

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return check_base_url(v)

    @field_validator("token")
    @classmethod
    def _token(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if not raw.strip() or re.search(r"\s", raw.strip()) or _CTRL.search(raw):
            raise ValueError("must be the access token only: no spaces, line breaks or empty value (copy the whole key)")
        return v

    @field_validator("automation_id_prefix", mode="before")
    @classmethod
    def _prefix(cls, v):
        v = v.strip() if isinstance(v, str) else v
        if not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,19}", v):
            raise ValueError("must be 1 to 20 characters: lower-case letters, digits and underscores, starting with a letter, for example claude_")
        return v

    @field_validator("ca_certificate")
    @classmethod
    def _ca(cls, v: str) -> str:
        if v:
            try:
                ssl.create_default_context().load_verify_locations(cadata=v)
            except (ssl.SSLError, ValueError):
                raise ValueError("must be one or more PEM certificates (-----BEGIN CERTIFICATE-----)") from None
        return v

    @field_validator("entity_allowlist", "entity_denylist", "write_entity_allowlist", mode="before")
    @classmethod
    def _globs(cls, v):
        return [norm_glob(x) for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else v

    @field_validator("domain_denylist", mode="before")
    @classmethod
    def _domains(cls, v):
        if not isinstance(v, list):
            return v
        out = [x.strip().lower() for x in v if isinstance(x, str) and x.strip()]
        if any(not re.fullmatch(r"[a-z0-9_]{1,64}", d) for d in out):
            raise ValueError("domains are lower-case letters, digits and '_' only, for example lock")
        return out

    @field_validator("allowed_services", mode="before")
    @classmethod
    def _services(cls, v):
        if not isinstance(v, list):
            return v
        out = [x.strip().lower() for x in v if isinstance(x, str) and x.strip()]
        if any(not SERVICE_RE.fullmatch(s) for s in out):
            raise ValueError("entries must look like light.turn_on (domain.service, no wildcards)")
        return out

    @field_validator("automation_categories", mode="before")
    @classmethod
    def _categories(cls, v):
        if not isinstance(v, list):
            return v
        out = [x.strip().casefold() for x in v if isinstance(x, str) and x.strip()]
        if len(out) > 20 or any(len(c) > 80 or _CTRL.search(c) for c in out):
            raise ValueError("at most 20 category names of up to 80 characters, no control characters")
        return out

    @field_validator("redact_attributes", mode="before")
    @classmethod
    def _redact(cls, v):
        return [x.strip().casefold()[:60] for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else v

    @model_validator(mode="after")
    def _services_vs_domains(self):
        denied = set(self.domain_denylist)
        if any(domain_of(s) in denied for s in self.allowed_services):
            raise ValueError("allowed_services contains a service in a domain listed in domain_denylist; remove one or the other")
        if any(domain_of(sv) == "automation" for sv in self.allowed_services):
            raise ValueError("allowed_services may not contain automation services; use automation_categories with the automation tools instead")
        if any(p.split(".", 1)[0] in denied for p in self.write_entity_allowlist):
            raise ValueError("write_entity_allowlist contains a pattern in a domain listed in domain_denylist; remove one or the other")
        return self


def _parse_time(value: str, name: str) -> datetime:
    value = value.strip()
    if len(value) > 40:
        raise ToolError(f"{name} is not a date and time. Use a form like 2026-11-06T08:00:00+00:00.")
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{name} is not a date and time. Use a form like 2026-11-06T08:00:00+00:00 (a time without an offset is taken as UTC).") from None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).replace(microsecond=0).isoformat()


class HomeAssistantPlugin(Plugin):
    def __init__(self, ctx, http: httpx2.AsyncClient) -> None:
        super().__init__(ctx)
        cfg = ctx.config
        self.policy = EntityPolicy.build(cfg.entity_allowlist, cfg.entity_denylist, cfg.domain_denylist)
        self.redact = frozenset(cfg.redact_attributes)
        self.write_allow = tuple(cfg.write_entity_allowlist)
        self._write_times: deque[float] = deque()  # per-process and disposable: a reload starts empty
        self.ha = HomeAssistant(http, self._settings, ctx.logger)
        self.instructions = (cfg.instructions or "").strip() or None  # the host passes this to the MCP server

    def _settings(self) -> Settings:
        c = self.ctx.config
        return Settings(c.base_url, c.token.get_secret_value(), float(c.timeout_seconds), c.max_response_bytes)  # read at the moment of use

    # ------------------------------------------------------------- plumbing
    @staticmethod
    async def _guard(coro):
        try:
            return await coro
        except HAError as e:
            raise ToolError(str(e)) from None

    def _entity_id(self, entity_id: str) -> str:
        e = (entity_id or "").strip().lower()
        if not ENTITY_RE.fullmatch(e):
            raise ToolError("entity_id must look like light.kitchen (lower-case letters, digits and underscores).")
        return e

    def _limit(self, limit: int) -> int:
        cap = self.ctx.config.max_results
        return cap if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 else min(limit, cap)

    async def _visible_states(self) -> dict[str, dict]:
        return {s["entity_id"]: s for s in await self.ha.states() if self.policy.visible(s["entity_id"])}

    async def _registry_or_none(self):
        try:
            return await self.ha.registry()
        except RegistryUnavailable:
            return None

    @staticmethod
    def _name(state: dict) -> str:
        attrs = state.get("attributes")
        return tx.clean(attrs.get("friendly_name", ""), 120) if isinstance(attrs, dict) and isinstance(attrs.get("friendly_name"), str) else ""

    def _line(self, s: dict, area: str = "") -> str:
        parts = [s["entity_id"], self._name(s) or "-", tx.clean(s.get("state", ""), 100)]
        if area:
            parts.append(f"area: {tx.clean(area, 80)}")
        return "- " + " | ".join(parts)

    # ---------------------------------------------------------------- tools
    @tool(tier="read")
    async def status(self) -> str:
        """Check the connection and token, and summarise what this connector can see: the Home Assistant version, visible entity counts by domain, and the policy in force."""
        info = await self._guard(self.ha.info())
        states = await self._visible_states_guarded()
        version = str(info.get("version", ""))
        version = version if re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", version) else "unknown"
        counts = Counter(domain_of(e) for e in states)
        try:
            await self.ha.registry()
            areas = "available"
        except RegistryUnavailable as e:
            areas = f"not available: {e}"
        cfg = self.ctx.config
        lines = [
            f"Connected to Home Assistant {version}; the access token works.",
            f"Visible entities: {len(states)}",
            "By domain: " + (", ".join(f"{d} {n}" for d, n in sorted(counts.items())) or "none"),
            f"Area information: {areas}",
            "Policy: " + (f"{len(cfg.entity_allowlist)} allow pattern(s); " if cfg.entity_allowlist else "no allow list (all entities not denied); ")
            + f"{len(cfg.entity_denylist)} deny pattern(s); denied domains: {', '.join(sorted(self.policy.deny_domains)) or 'none'}. Anything the policy hides is neither shown nor counted.",
            f"call_service: {len(cfg.allowed_services)} service(s) allowed; {len(cfg.write_entity_allowlist)} entity pattern(s) it may act on"
            + (" (none: nothing can be changed)." if not cfg.write_entity_allowlist or not cfg.allowed_services else ".")
            + " It is only available if the owner ticked it under Policy.",
            f"Automation definition tools need the token's user to be an administrator; ids Claude may create or change start with {cfg.automation_id_prefix}.",
            "Automations Claude may trigger or switch: " + (f"categories {', '.join(cfg.automation_categories)}" if cfg.automation_categories else "no category set")
            + (f" plus {len(cfg.write_entity_allowlist)} write pattern(s)" if cfg.write_entity_allowlist else "") + ". Only if the owner ticked the automation tools under Policy.",
        ]
        return "\n".join(lines)

    async def _visible_states_guarded(self) -> dict[str, dict]:
        return await self._guard(self._visible_states())

    @tool(tier="read", untrusted=True)
    async def search_entities(self, query: str = "", domain: str = "", area: str = "", device_class: str = "", state: str = "", limit: int = 0, offset: int = 0) -> str:
        """Search visible entities. query: words that must all appear in the entity id or name. Filters: domain (such as light), area (name), device_class (such as temperature), state (such as on). limit and offset page the results."""
        query, domain, area, device_class, state = (str(x or "").strip().lower() for x in (query, domain, area, device_class, state))
        if max(len(query), len(domain), len(area), len(device_class), len(state)) > 100:
            raise ToolError("Search values are limited to 100 characters.")
        limit, offset = self._limit(limit), max(0, offset if isinstance(offset, int) and not isinstance(offset, bool) else 0)
        states = await self._visible_states_guarded()
        registry = None
        if area:
            try:
                registry = await self._guard(self.ha.registry())
            except ToolError:
                raise ToolError("Filtering by area needs area data, which is not available right now. Run status to see why.") from None
        words = query.split()
        hits = []
        for eid in sorted(states):
            s = states[eid]
            attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
            hay = f"{eid} {self._name(s)}".lower()
            if domain and domain_of(eid) != domain:
                continue
            if words and not all(w in hay for w in words):
                continue
            if device_class and str(attrs.get("device_class", "")).lower() != device_class:
                continue
            if state and str(s.get("state", "")).lower() != state:
                continue
            if area and registry is not None:
                a = registry.entity_area.get(eid)
                if not a or area not in (a.lower(), registry.areas.get(a, "").lower()):
                    continue
            hits.append(eid)
        page = hits[offset: offset + limit]
        reg = registry if registry is not None else (await self._registry_or_none() if page else None)
        head = f"{len(hits)} entities match (showing {offset + 1 if page else 0} to {offset + len(page)})."
        if offset + len(page) < len(hits):
            head += f" Use offset {offset + len(page)} for the next page."
        return "\n".join([head, *(self._line(states[e], reg.area_name(e) if reg else "") for e in page)])

    @tool(tier="read", untrusted=True)
    async def get_entity(self, entity_id: str) -> str:
        """Get one entity: state, name, area, device class, timestamps and attributes."""
        eid = self._entity_id(entity_id)
        states = await self._visible_states_guarded()
        s = states.get(eid)
        if s is None:
            return "No such entity."  # a hidden entity and a missing one give this same answer
        attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
        reg = await self._registry_or_none()
        lines = [f"entity_id: {eid}", f"name: {self._name(s) or '-'}", f"state: {tx.clean(s.get('state', ''), 255)}"]
        if reg and reg.area_name(eid):
            lines.append(f"area: {tx.clean(reg.area_name(eid), 80)}")
        if attrs.get("device_class"):
            lines.append(f"device_class: {tx.clean(attrs['device_class'], 60)}")
        lines += [f"last_changed: {tx.clean(s.get('last_changed', ''), 40)}", f"last_updated: {tx.clean(s.get('last_updated', ''), 40)}", "attributes:"]
        lines += tx.attributes(attrs, self.policy, self.redact) or ["  (none)"]
        return "\n".join(lines)

    @tool(tier="read", untrusted=True)
    async def get_states(self, entity_ids: list[str]) -> str:
        """Get the state of several named entities at once (at most 50). Entities that do not exist, and entities hidden by policy, are reported alike as not found."""
        if not isinstance(entity_ids, list) or not entity_ids:
            raise ToolError("Give a list of entity ids.")
        if len(entity_ids) > 50:
            raise ToolError("At most 50 entities per call.")
        wanted = list(dict.fromkeys(self._entity_id(e) for e in entity_ids))
        states = await self._visible_states_guarded()
        reg = await self._registry_or_none()
        return "\n".join(self._line(states[e], reg.area_name(e) if reg else "") if e in states else f"- {e} | not found" for e in wanted)

    def _window(self, start: str, end: str, hours: int) -> tuple[datetime, datetime]:
        now = datetime.now(UTC)
        stop = _parse_time(end, "end") if end.strip() else now
        hours = hours if isinstance(hours, int) and not isinstance(hours, bool) and hours > 0 else 24
        begin = _parse_time(start, "start") if start.strip() else stop - timedelta(hours=hours)
        if begin >= stop:
            raise ToolError("start must be earlier than end.")
        limit = self.ctx.config.max_history_hours
        if stop - begin > timedelta(hours=limit):
            raise ToolError(f"That window is longer than this connector allows ({limit} hours, the max_history_hours setting). Ask for a shorter one.")
        return begin, stop

    @tool(tier="read", untrusted=True)
    async def get_history(self, entity_id: str, start: str = "", end: str = "", hours: int = 24) -> str:
        """State changes of one entity over a time window. Give start and end as ISO 8601 times (UTC if no offset), or just hours back from end (default 24). Only significant changes are returned, at most 200."""
        eid = self._entity_id(entity_id)
        begin, stop = self._window(start, end, hours)
        none = f"No history found for {eid} in that window."
        if not self.policy.visible(eid):
            return none
        rows = await self._guard(self.ha.history(eid, _iso(begin), _iso(stop)))
        if not rows:
            return none
        shown = rows[:HISTORY_POINTS]
        lines = [f"History of {eid} from {_iso(begin)} to {_iso(stop)} (UTC): {len(rows)} change(s)" + (f", showing the first {HISTORY_POINTS}; use a shorter window." if len(rows) > HISTORY_POINTS else ".")]
        lines += [f"- {tx.clean(r.get('last_changed') or r.get('last_updated') or '', 40)} | {tx.clean(r.get('state', ''), 100)}" for r in shown]
        return "\n".join(lines)

    @tool(tier="read", untrusted=True)
    async def get_logbook(self, entity_id: str, start: str = "", end: str = "", hours: int = 24) -> str:
        """Logbook entries for one entity over a time window (ISO 8601 times, or hours back from end). At most 100 entries. Context details that could refer to other entities are not returned."""
        eid = self._entity_id(entity_id)
        begin, stop = self._window(start, end, hours)
        none = f"No logbook entries for {eid} in that window."
        if not self.policy.visible(eid):
            return none
        rows = await self._guard(self.ha.logbook(eid, _iso(begin), _iso(stop)))
        known = frozenset(domain_of(s["entity_id"]) for s in await self._guard(self.ha.states()))
        kept = []
        for r in rows:
            name, message = str(r.get("name", "")), str(r.get("message", ""))
            other = r.get("entity_id")
            if isinstance(other, str) and other.lower() != eid:
                continue  # only the entity that was asked for
            if self.policy.mentions_hidden(name, known) or self.policy.mentions_hidden(message, known):
                continue
            kept.append(f"- {tx.clean(r.get('when', ''), 40)} | {tx.clean(name, 80)} | {tx.clean(message, 200)}")
        if not kept:
            return none
        head = f"Logbook of {eid} from {_iso(begin)} to {_iso(stop)} (UTC): {len(kept)} entr{'y' if len(kept) == 1 else 'ies'}" + (f", showing the first {LOGBOOK_ENTRIES}." if len(kept) > LOGBOOK_ENTRIES else ".")
        return "\n".join([head, *kept[:LOGBOOK_ENTRIES]])

    @tool(tier="read", untrusted=True)
    async def list_areas(self, area: str = "", limit: int = 0, offset: int = 0) -> str:
        """List areas with the number of visible entities in each, or, with area (a name), list the visible entities in that area (paged)."""
        area = str(area or "").strip().lower()
        if len(area) > 100:
            raise ToolError("The area name is limited to 100 characters.")
        limit, offset = self._limit(limit), max(0, offset if isinstance(offset, int) and not isinstance(offset, bool) else 0)
        states = await self._visible_states_guarded()
        reg = await self._guard(self.ha.registry())
        members: dict[str, list[str]] = {a: [] for a in reg.areas}
        for eid in sorted(states):
            a = reg.entity_area.get(eid)
            if a in members:
                members[a].append(eid)
        if not area:
            rows = sorted(reg.areas.items(), key=lambda kv: kv[1].lower())
            return "\n".join([f"{len(rows)} areas.", *(f"- {tx.clean(n, 80)} (id {tx.clean(a, 60)}): {len(members[a])} entities" for a, n in rows[offset: offset + limit])]) if rows else "No areas."
        ids = [a for a, n in reg.areas.items() if area in (a.lower(), n.lower())]
        eids = sorted(e for a in ids for e in members[a])
        page = eids[offset: offset + limit]
        head = f"{len(eids)} entities in that area (showing {offset + 1 if page else 0} to {offset + len(page)})."
        if offset + len(page) < len(eids):
            head += f" Use offset {offset + len(page)} for the next page."
        return "\n".join([head, *(self._line(states[e]) for e in page)])

    @tool(tier="read", untrusted=True)
    async def list_services(self, domain: str = "") -> str:
        """List the services call_service is configured to allow, with their descriptions, and which entities it may act on. It lists only; to act, use call_service (if the owner has enabled it)."""
        cfg = self.ctx.config
        allowed = [s for s in cfg.allowed_services if self.policy.service_allowed(s, tuple(cfg.allowed_services))]
        domain = str(domain or "").strip().lower()
        if domain:
            allowed = [s for s in allowed if domain_of(s) == domain]
        if not allowed:
            return "No services are allowed. This connector cannot call services."
        known = await self._guard(self.ha.services())
        lines = [f"{len(allowed)} allowed service(s). Entities call_service may act on: " + (", ".join(cfg.write_entity_allowlist) if cfg.write_entity_allowlist else "none") + "."]
        for s in allowed:
            d, name = s.split(".", 1)
            desc = known.get(d, {}).get(name)
            if desc is None:
                lines.append(f"- {s} | not offered by Home Assistant")
                continue
            fields = desc.get("fields") if isinstance(desc.get("fields"), dict) else {}
            lines.append(f"- {s} | {tx.clean(desc.get('description') or desc.get('name') or '', 200)}" + (f" | fields: {', '.join(tx.clean(f, 40) for f in list(fields)[:20])}" if fields else ""))
        return "\n".join(lines)

    # ---------------------------------------------------------------- write
    # Off twice over: the host serves a write tool only when the owner ticks it by name, and even then allowed_services and
    # write_entity_allowlist must both name what may be done. Each refusal happens before anything is sent to Home Assistant.
    @tool(tier="write", untrusted=True)
    async def call_service(self, domain: str, service: str, entity_id: str, data: dict | None = None) -> str:
        """Call one Home Assistant service on ONE named entity, for example domain light, service turn_on, entity_id light.kitchen, data {"brightness": 120}. Only services the owner allowed, on entities the owner allowed. There are no group, area or "all" targets. Returns the entity's state before and after."""
        cfg = self.ctx.config
        d, sv = str(domain or "").strip().lower(), str(service or "").strip().lower()
        pair = f"{d}.{sv}"
        no_service = "No such service."  # one answer for a service that is not allowed and one that does not exist
        if not SERVICE_RE.fullmatch(pair) or not self.policy.service_allowed(pair, tuple(cfg.allowed_services)):
            return self._refuse(no_service)
        eid = self._entity_id(entity_id)
        if domain_of(eid) != d:
            raise ToolError("The entity must be in the same domain as the service (a light service acts on a light).")
        if eid not in await self._visible_states_guarded():
            return self._refuse("No such entity.")  # hidden and missing look the same, whatever the write list says
        if not writable(self.policy, self.write_allow, eid):
            return self._refuse("This connector may not change that entity (it is not on the write list set by the owner).")
        problem = check_service_data(data if data is not None else {}, eid)
        if problem:
            raise ToolError(problem)
        known = await self._guard(self.ha.services())
        if sv not in known.get(d, {}):
            return self._refuse(no_service)
        before = await self._guard(self.ha.state(eid))
        if before is None:
            return self._refuse("No such entity.")
        now, limit = time.monotonic(), cfg.max_writes_per_minute
        while self._write_times and now - self._write_times[0] > 60:
            self._write_times.popleft()
        if len(self._write_times) >= limit:
            raise ToolError(f"Too many service calls in the last minute ({limit}, the max_writes_per_minute setting). Wait and try again.")
        self._write_times.append(now)
        changed = await self._guard(self.ha.call_service(d, sv, {**(data or {}), "entity_id": eid}))
        after = await self._guard(self.ha.state(eid))
        self.ctx.logger.info("service called: %s on %s", pair, eid)
        stats = getattr(self.ctx, "stats", None)
        if stats is not None:
            stats.count("service_calls", 1)
        also = sorted({c["entity_id"] for c in changed if isinstance(c.get("entity_id"), str) and c["entity_id"] != eid and self.policy.visible(c["entity_id"])})
        lines = [f"Called {pair} on {eid}.", f"before: {self._brief(before)}", f"after: {self._brief(after) if after else 'unknown'}"]
        if also:
            lines.append("other visible entities that changed: " + ", ".join(also[:20]) + (f" and {len(also) - 20} more" if len(also) > 20 else ""))
        lines.append("A device can take a moment to report its new state; run get_entity to confirm if it still shows the old one.")
        return "\n".join(lines)

    # ----------------------------------------------------------- automations
    def _take_write_slot(self) -> None:
        cfg = self.ctx.config
        now = time.monotonic()
        while self._write_times and now - self._write_times[0] > 60:
            self._write_times.popleft()
        if len(self._write_times) >= cfg.max_writes_per_minute:
            raise ToolError(f"Too many service calls in the last minute ({cfg.max_writes_per_minute}, the max_writes_per_minute setting). Wait and try again.")
        self._write_times.append(now)

    async def _automation_may_run(self, eid: str, registry) -> bool:
        """An automation may be controlled when it is in an allowed category, or matches the write list."""
        cats = frozenset(self.ctx.config.automation_categories)
        if writable(self.policy, self.write_allow, eid):
            return True
        return bool(registry is not None and cats and registry.automation_category(eid).casefold() in cats)

    async def _automation_target(self, entity_id: str) -> str:
        eid = self._entity_id(entity_id)
        if domain_of(eid) != "automation":
            raise ToolError("entity_id must be an automation, like automation.porch_lights.")
        if eid not in await self._visible_states_guarded():
            return self._refuse("No such entity.")  # hidden and missing look the same
        registry = await self._registry_or_none()
        if not await self._automation_may_run(eid, registry):
            note = "" if registry is not None else " Category information is not available right now (run status), so only automations on the write list can be controlled."
            return self._refuse("This connector may not control that automation (it is not in a category the owner allowed)." + note)
        return eid

    def _auto_brief(self, s: dict) -> str:
        attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
        return f"{tx.clean(s.get('state', ''), 40)}, last run {tx.clean(attrs.get('last_triggered') or 'never', 40)}, running {tx.clean(attrs.get('current', 0), 10)}"

    async def _automation_call(self, eid: str, service: str, data: dict, headline: str) -> str:
        before = await self._guard(self.ha.state(eid))
        if before is None:
            return self._refuse("No such entity.")
        self._take_write_slot()
        changed = await self._guard(self.ha.call_service("automation", service, {**data, "entity_id": eid}))
        after = await self._guard(self.ha.state(eid))
        self.ctx.logger.info("automation service called: automation.%s on %s", service, eid)
        stats = getattr(self.ctx, "stats", None)
        if stats is not None:
            stats.count("service_calls", 1)
        also = sorted({c["entity_id"] for c in changed if isinstance(c.get("entity_id"), str) and c["entity_id"] != eid and self.policy.visible(c["entity_id"])})
        lines = [headline, f"before: {self._auto_brief(before)}", f"after: {self._auto_brief(after) if after else 'unknown'}"]
        if also:
            lines.append("other visible entities that changed: " + ", ".join(also[:20]) + (f" and {len(also) - 20} more" if len(also) > 20 else ""))
        lines.append("An automation's actions can take a moment and can change things this connector cannot show; check with get_entity or the logbook.")
        return "\n".join(lines)

    @tool(tier="read", untrusted=True)
    async def list_automations(self, category: str = "", state: str = "", limit: int = 0, offset: int = 0) -> str:
        """List automations: id, name, on or off, last run, how many runs are active, category, and whether this connector may control it. Filter by category (name) or state (on or off)."""
        category, state = str(category or "").strip().casefold(), str(state or "").strip().lower()
        if max(len(category), len(state)) > 100:
            raise ToolError("Search values are limited to 100 characters.")
        limit, offset = self._limit(limit), max(0, offset if isinstance(offset, int) and not isinstance(offset, bool) else 0)
        states = {e: s for e, s in (await self._visible_states_guarded()).items() if domain_of(e) == "automation"}
        registry = None
        if category:
            try:
                registry = await self._guard(self.ha.registry())
            except ToolError:
                raise ToolError("Filtering by category needs category data, which is not available right now. Run status to see why.") from None
        else:
            registry = await self._registry_or_none()
        hits = [e for e in sorted(states) if (not state or str(states[e].get("state", "")).lower() == state)
                and (not category or (registry is not None and registry.automation_category(e).casefold() == category))]
        page = hits[offset: offset + limit]
        head = f"{len(hits)} automations (showing {offset + 1 if page else 0} to {offset + len(page)})."
        if offset + len(page) < len(hits):
            head += f" Use offset {offset + len(page)} for the next page."
        rows = []
        for e in page:
            s = states[e]
            attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
            cat = registry.automation_category(e) if registry is not None else ""
            may = "yes" if await self._automation_may_run(e, registry) else "no"
            rows.append(f"- {e} | {self._name(s) or '-'} | {tx.clean(s.get('state', ''), 20)} | last run {tx.clean(attrs.get('last_triggered') or 'never', 40)} | running {tx.clean(attrs.get('current', 0), 10)}"
                        + (f" | category: {tx.clean(cat, 60)}" if cat else "") + f" | controllable: {may}")
        return "\n".join([head, *rows])

    @tool(tier="write", untrusted=True)
    async def trigger_automation(self, entity_id: str) -> str:
        """Run one automation now (only one in a category the owner allowed). The automation's own conditions are respected: if they are not met, it does nothing. Returns its state and last run before and after."""
        eid = await self._automation_target(entity_id)
        return await self._automation_call(eid, "trigger", {"skip_condition": False}, f"Triggered {eid} (its conditions still apply).")

    @tool(tier="write", untrusted=True)
    async def set_automation_enabled(self, entity_id: str, enabled: bool) -> str:
        """Switch one automation on (enabled true) or off (enabled false), only one in a category the owner allowed. Disabling does not stop a run that is already in progress."""
        if not isinstance(enabled, bool):
            raise ToolError("enabled must be true or false.")
        eid = await self._automation_target(entity_id)
        if enabled:
            return await self._automation_call(eid, "turn_on", {}, f"Enabled {eid}.")
        return await self._automation_call(eid, "turn_off", {"stop_actions": False}, f"Disabled {eid} (a run already in progress is left to finish).")

    # ------------------------------------------------- automation definitions
    def _config_id(self, automation_id: str) -> str:
        cid = str(automation_id or "").strip().lower()
        prefix = self.ctx.config.automation_id_prefix
        if not re.fullmatch(re.escape(prefix) + ID_BODY, cid):
            raise ToolError(f"automation_id must start with {prefix} followed by 1 to 40 lower-case letters, digits or underscores (for example {prefix}porch_evening). "
                            "Only automations whose id starts that way can be created, changed or deleted by this connector.")
        return cid

    @tool(tier="read", untrusted=True)
    async def get_automation_definition(self, entity_id: str) -> str:
        """Show the triggers, conditions and actions of one automation (needs the token's user to be an administrator). Definitions that contain templates or name something this connector may not see are not shown."""
        eid = self._entity_id(entity_id)
        none = "No definition available."  # hidden, missing, templated and unreadable definitions all give this
        states = await self._visible_states_guarded()
        s = states.get(eid)
        if s is None or domain_of(eid) != "automation":
            return none
        attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
        cid = attrs.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", cid):
            return none
        config = await self._guard(self.ha.automation_config(cid))
        if config is None:
            return none
        known = frozenset(domain_of(e["entity_id"]) for e in await self._guard(self.ha.states()))
        if definition_problem(config, lambda text: self.policy.mentions_hidden(text, known)):
            return none
        text = json.dumps(config, ensure_ascii=True, indent=1)  # ensure_ascii escapes any control character
        return f"definition of {eid} (config id {cid}):\n" + (text if len(text) <= 6000 else text[:6000] + "\n[truncated]")

    @tool(tier="write", untrusted=True)
    async def save_automation(self, automation_id: str, alias: str, triggers: list[dict], actions: list[dict], conditions: list[dict] | None = None,
                              description: str = "", mode: str = "single") -> str:
        """Create or replace an automation whose id starts with the owner's prefix (default claude_). Only simple triggers (state, numeric_state, time, time_pattern, sun), conditions (state, numeric_state, time, sun) and actions (an allowed service on one allowed entity, or a delay); no templates. New automations start disabled. Triggers and conditions are like {"type": "state", "entity_id": "binary_sensor.door", "to": "on"}; actions like {"service": "light.turn_on", "entity_id": "light.porch", "data": {"brightness": 120}} or {"delay": "00:00:10"}."""
        cfg = self.ctx.config
        cid = self._config_id(automation_id)
        states = await self._visible_states_guarded()
        allowed = tuple(sv for sv in cfg.allowed_services if self.policy.service_allowed(sv, tuple(cfg.allowed_services)))
        ctx = Context(self.policy.visible, lambda e: e in states, lambda e: writable(self.policy, self.write_allow, e), allowed)
        try:
            config = build_config(cid, alias, description, mode, triggers, conditions, actions, ctx)
        except DefinitionError as e:
            raise ToolError(str(e)) from None
        for e, st in states.items():
            a = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
            if domain_of(e) == "automation" and self._name(st).casefold() == config["alias"].casefold() and a.get("id") != cid:
                raise ToolError("An automation with that name already exists. Choose a different alias.")
        existing = await self._guard(self.ha.automation_config(cid))
        if existing is None:
            config["initial_state"] = False  # a new automation does nothing until the owner enables it
        elif "initial_state" in existing:
            config["initial_state"] = existing["initial_state"]  # keep the owner's choice on later edits
        self._take_write_slot()
        await self._guard(self.ha.save_automation_config(cid, config))
        self.ctx.logger.info("automation saved: %s (%s)", cid, "updated" if existing is not None else "created")
        stats = getattr(self.ctx, "stats", None)
        if stats is not None:
            stats.count("automation_saves", 1)
        entity = ""
        for _ in range(6):  # Home Assistant reloads the automation and gives it an entity a moment later
            for st in await self._guard(self.ha.states(fresh=True)):
                a = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
                if domain_of(st["entity_id"]) == "automation" and a.get("id") == cid and self.policy.visible(st["entity_id"]):
                    entity = st["entity_id"]
                    break
            if entity:
                break
            await asyncio.sleep(0.5)
        lines = [f"{'Updated' if existing is not None else 'Created'} automation {cid} ({tx.clean(config['alias'], 100)}).",
                 f"entity: {entity or 'not visible yet (Home Assistant is still reloading)'}",
                 f"{len(config['triggers'])} trigger(s), {len(config.get('conditions', []))} condition(s), {len(config['actions'])} action(s), mode {config['mode']}.",
                 ("It starts DISABLED: it does nothing until the owner enables it in Home Assistant. A later edit keeps that setting." if existing is None
                  else f"initial_state is {'kept as the owner set it' if 'initial_state' in config else 'not set, so the owner controls whether it runs'}."),
                 "Home Assistant rewrites automations.yaml when saving, so comments in that file are not kept."]
        return "\n".join(lines)

    @tool(tier="admin")
    async def delete_automation(self, automation_id: str) -> str:
        """Delete an automation whose id starts with the owner's prefix (default claude_). Others cannot be deleted. This cannot be undone."""
        cid = self._config_id(automation_id)
        if await self._guard(self.ha.automation_config(cid)) is None:
            return "No such automation."
        self._take_write_slot()
        await self._guard(self.ha.delete_automation_config(cid))
        self.ctx.logger.info("automation deleted: %s", cid)
        stats = getattr(self.ctx, "stats", None)
        if stats is not None:
            stats.count("automation_deletes", 1)
        return f"Deleted automation {cid}. Home Assistant rewrites automations.yaml, so comments in that file are not kept."

    def _brief(self, s: dict) -> str:
        attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
        shown = [f"{tx.clean(k, 40)}={tx.clean(attrs[k], 60)}" for k in ("brightness", "temperature", "current_temperature", "current_position", "hvac_mode") if k in attrs]
        return f"{tx.clean(s.get('state', ''), 100)}" + (f" ({', '.join(shown)})" if shown else "")

    @staticmethod
    def _refuse(message: str) -> str:
        raise ToolError(message)


async def setup(host: PluginHost) -> None:
    ctx = host.context
    cfg = ctx.config
    http = httpx2.AsyncClient(verify=ssl_context(cfg.verify_tls, cfg.ca_certificate), timeout=httpx2.Timeout(float(cfg.timeout_seconds)),
                              follow_redirects=False, limits=httpx2.Limits(max_connections=4))
    await host.add_plugin(HomeAssistantPlugin(ctx, http))  # no network work here: problems surface on use, as a tool error that says what to fix


async def teardown(host: PluginHost) -> None:
    plugin = next(iter(getattr(host, "_registered", [])), None)
    if plugin is not None:
        await plugin.ha.aclose()

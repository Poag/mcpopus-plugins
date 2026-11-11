"""Building and checking automation definitions. The saved config is built here from a strict whitelist, never passed through.

An automation Claude writes runs later with no plugin policy in the loop, so what may be saved is the control: a small set of trigger,
condition and action types, only on entities and services the owner allowed, no templates, nothing that can run other logic.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from .policy import ENTITY_RE, SERVICE_RE, check_service_data, domain_of

ID_BODY = r"[a-z0-9_]{1,40}"
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
OFFSET_RE = re.compile(r"^-?\d{1,2}:\d{2}(:\d{2})?$")
PATTERN_RE = re.compile(r"^(\*|\d{1,2}|/\d{1,2})$")
ATTR_RE = re.compile(r"^[a-z0-9_]{1,64}$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LIMITS = {"triggers": 5, "conditions": 5, "actions": 10}
MAX_DELAY_SECONDS = 3600


class DefinitionError(Exception):
    """Why a definition was refused. The message never repeats free text from the caller beyond an entity id or a key name."""


def has_template(value: Any, depth: int = 0) -> bool:
    if isinstance(value, str):
        return "{{" in value or "{%" in value or "{#" in value
    if isinstance(value, (list, tuple)) and depth < 6:
        return any(has_template(v, depth + 1) for v in value)
    if isinstance(value, dict) and depth < 6:
        return any(has_template(k, depth + 1) or has_template(v, depth + 1) for k, v in value.items())
    return False


def _scalar(v: Any, what: str, limit: int = 64) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise DefinitionError(f"{what} must be text or a number.")
    s = str(v)
    if not s or len(s) > limit or re.search(r"[\x00-\x1f\x7f]", s) or has_template(s):
        raise DefinitionError(f"{what} is empty, too long (over {limit} characters), has control characters, or contains a template (not allowed).")
    return s


def _number(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or abs(float(v)) > 1e9:
        raise DefinitionError(f"{what} must be a number.")
    return v


def _time(v: Any, what: str, offset: bool = False) -> str:
    if not isinstance(v, str) or not (OFFSET_RE if offset else TIME_RE).fullmatch(v.strip()):
        raise DefinitionError(f"{what} must be a time like 18:30 or 18:30:00" + (" (a sun offset may start with -)." if offset else "."))
    return v.strip()


def _duration(v: Any, what: str, maximum: int = 86_400) -> str:
    if isinstance(v, bool):
        raise DefinitionError(f"{what} must be a time like 00:05:00 or a number of seconds.")
    if isinstance(v, (int, float)):
        seconds = int(v)
    elif isinstance(v, str) and TIME_RE.fullmatch(v.strip()):
        parts = [int(p) for p in v.strip().split(":")]  # Home Assistant reads HH:MM:SS, and HH:MM when there are two parts
        seconds = parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) == 3 else 0)
    else:
        raise DefinitionError(f"{what} must be a time like 00:05:00 or a number of seconds.")
    if not 0 <= seconds <= maximum:
        raise DefinitionError(f"{what} is longer than allowed ({maximum} seconds).")
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Context:
    """What the checks need to know, supplied by the plugin."""

    def __init__(self, visible: Callable[[str], bool], exists: Callable[[str], bool], writable: Callable[[str], bool], services: tuple[str, ...],
                 hidden_by: Callable[[str], str | None] | None = None) -> None:
        self.visible, self.exists, self.writable, self.services = visible, exists, writable, services
        self.hidden_by = hidden_by or (lambda _eid: None)

    def _unavailable(self, eid: str, what: str) -> DefinitionError:
        """Why an entity cannot be used. A policy rule is named because it depends on the id and the settings alone: an id that fails the policy
        gets the same answer whether or not Home Assistant has it, so what exists behind the policy is not revealed."""
        rule = self.hidden_by(eid)
        if rule == "domain":
            return DefinitionError(f"The entity {eid} in {what} cannot be used: its domain ({eid.split('.', 1)[0]}) is in the domain_denylist setting.")
        if rule == "deny":
            return DefinitionError(f"The entity {eid} in {what} cannot be used: it matches the entity_denylist setting.")
        if rule == "allow":
            return DefinitionError(f"The entity {eid} in {what} cannot be used: it is outside the entity_allowlist setting, so it is hidden "
                                   "(that list is the read filter: anything it does not match is invisible. Run status to see whether an allow list is set).")
        return DefinitionError(f"The entity {eid} in {what} does not exist in Home Assistant (or is not available).")

    def entity(self, value: Any, what: str, multiple: bool = True) -> list[str]:
        items = value if isinstance(value, list) else [value]
        if not items or len(items) > (10 if multiple else 1) or (isinstance(value, list) and not multiple):
            raise DefinitionError(f"{what} needs " + ("one to ten entity ids." if multiple else "exactly one entity id."))
        out = []
        for e in items:
            eid = e.strip().lower() if isinstance(e, str) else ""
            if not ENTITY_RE.fullmatch(eid):
                raise DefinitionError(f"{what} must name entities like light.kitchen.")
            if not self.visible(eid) or not self.exists(eid):
                raise self._unavailable(eid, what)
            out.append(eid)
        return out


def _only(item: dict, allowed: set[str], what: str) -> None:
    extra = sorted(str(k) for k in item if k not in allowed)
    if extra:
        raise DefinitionError(f"{what} does not support the field {extra[0][:40]!r}. Supported: {', '.join(sorted(allowed))}.")


def _entity_value(item: dict, ctx: Context, what: str) -> list[str]:
    if "entity_id" not in item:
        raise DefinitionError(f"{what} needs entity_id.")
    return ctx.entity(item["entity_id"], what)


def _trigger(t: Any, ctx: Context) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(t, dict) or t.get("type") not in ("state", "numeric_state", "time", "time_pattern", "sun"):
        raise DefinitionError("Each trigger needs a type: state, numeric_state, time, time_pattern or sun.")
    kind = t["type"]
    out: dict[str, Any] = {"trigger": kind}
    watched: list[str] = []
    if kind == "state":
        _only(t, {"type", "entity_id", "to", "from", "not_to", "not_from", "for", "attribute"}, "A state trigger")
        watched = _entity_value(t, ctx, "a state trigger")
        out["entity_id"] = watched if len(watched) > 1 else watched[0]
        for key in ("to", "from", "not_to", "not_from"):
            if key in t:
                out[key] = _scalar(t[key], f"{key} in a state trigger")
        if "for" in t:
            out["for"] = _duration(t["for"], "for in a state trigger")
        if "attribute" in t:
            out["attribute"] = _attr(t["attribute"])
    elif kind == "numeric_state":
        _only(t, {"type", "entity_id", "above", "below", "for", "attribute"}, "A numeric_state trigger")
        watched = _entity_value(t, ctx, "a numeric_state trigger")
        out["entity_id"] = watched if len(watched) > 1 else watched[0]
        if "above" not in t and "below" not in t:
            raise DefinitionError("A numeric_state trigger needs above or below.")
        for key in ("above", "below"):
            if key in t:
                out[key] = _number(t[key], key)
        if "for" in t:
            out["for"] = _duration(t["for"], "for in a numeric_state trigger")
        if "attribute" in t:
            out["attribute"] = _attr(t["attribute"])
    elif kind == "time":
        _only(t, {"type", "at"}, "A time trigger")
        out["at"] = _time(t.get("at"), "at in a time trigger")
    elif kind == "time_pattern":
        _only(t, {"type", "hours", "minutes", "seconds"}, "A time_pattern trigger")
        if not any(k in t for k in ("hours", "minutes", "seconds")):
            raise DefinitionError("A time_pattern trigger needs hours, minutes or seconds.")
        for key in ("hours", "minutes", "seconds"):
            if key in t:
                if not isinstance(t[key], (str, int)) or isinstance(t[key], bool) or not PATTERN_RE.fullmatch(str(t[key])):
                    raise DefinitionError(f"{key} in a time_pattern trigger must be a number, * or /N (for example /5).")
                out[key] = str(t[key])
    else:
        _only(t, {"type", "event", "offset"}, "A sun trigger")
        if t.get("event") not in ("sunrise", "sunset"):
            raise DefinitionError("A sun trigger needs event sunrise or sunset.")
        out["event"] = t["event"]
        if "offset" in t:
            out["offset"] = _time(t["offset"], "offset", offset=True)
    return out, watched


def _attr(v: Any) -> str:
    if not isinstance(v, str) or not ATTR_RE.fullmatch(v):
        raise DefinitionError("attribute must be a simple name such as brightness.")
    return v


def _condition(c: Any, ctx: Context) -> dict[str, Any]:
    if not isinstance(c, dict) or c.get("type") not in ("state", "numeric_state", "time", "sun"):
        raise DefinitionError("Each condition needs a type: state, numeric_state, time or sun.")
    kind = c["type"]
    out: dict[str, Any] = {"condition": kind}
    if kind == "state":
        _only(c, {"type", "entity_id", "state", "for", "attribute"}, "A state condition")
        ents = _entity_value(c, ctx, "a state condition")
        out["entity_id"] = ents if len(ents) > 1 else ents[0]
        if "state" not in c:
            raise DefinitionError("A state condition needs state.")
        st = c["state"]
        out["state"] = [_scalar(x, "state in a state condition") for x in st[:10]] if isinstance(st, list) and st else _scalar(st, "state in a state condition")
        if "for" in c:
            out["for"] = _duration(c["for"], "for in a state condition")
        if "attribute" in c:
            out["attribute"] = _attr(c["attribute"])
    elif kind == "numeric_state":
        _only(c, {"type", "entity_id", "above", "below", "attribute"}, "A numeric_state condition")
        ents = _entity_value(c, ctx, "a numeric_state condition")
        out["entity_id"] = ents if len(ents) > 1 else ents[0]
        if "above" not in c and "below" not in c:
            raise DefinitionError("A numeric_state condition needs above or below.")
        for key in ("above", "below"):
            if key in c:
                out[key] = _number(c[key], key)
        if "attribute" in c:
            out["attribute"] = _attr(c["attribute"])
    elif kind == "time":
        _only(c, {"type", "after", "before", "weekday"}, "A time condition")
        if not any(k in c for k in ("after", "before", "weekday")):
            raise DefinitionError("A time condition needs after, before or weekday.")
        for key in ("after", "before"):
            if key in c:
                out[key] = _time(c[key], f"{key} in a time condition")
        if "weekday" in c:
            wd = c["weekday"]
            if not isinstance(wd, list) or not wd or len(wd) > 7 or any(d not in WEEKDAYS for d in wd):
                raise DefinitionError("weekday is a list of mon, tue, wed, thu, fri, sat, sun.")
            out["weekday"] = list(dict.fromkeys(wd))
    else:
        _only(c, {"type", "after", "before", "after_offset", "before_offset"}, "A sun condition")
        if not any(k in c for k in ("after", "before")):
            raise DefinitionError("A sun condition needs after or before (sunrise or sunset).")
        for key in ("after", "before"):
            if key in c:
                if c[key] not in ("sunrise", "sunset"):
                    raise DefinitionError(f"{key} in a sun condition is sunrise or sunset.")
                out[key] = c[key]
        for key in ("after_offset", "before_offset"):
            if key in c:
                out[key] = _time(c[key], key, offset=True)
    return out


def _action(a: Any, ctx: Context) -> tuple[dict[str, Any], str | None]:
    """Returns the action in Home Assistant's format and the entity it changes (None for a delay)."""
    if isinstance(a, dict) and "delay" in a:
        _only(a, {"delay"}, "A delay")
        return {"delay": _duration(a["delay"], "delay", MAX_DELAY_SECONDS)}, None
    if not isinstance(a, dict) or "service" not in a:
        raise DefinitionError("Each action is either {\"delay\": \"00:00:05\"} or {\"service\": \"light.turn_on\", \"entity_id\": \"light.kitchen\", \"data\": {...}}.")
    _only(a, {"service", "entity_id", "data"}, "A service action")
    svc = a["service"].strip().lower() if isinstance(a["service"], str) else ""
    if not SERVICE_RE.fullmatch(svc) or svc not in ctx.services:
        raise DefinitionError("That service is not on the owner's allowed_services list (or does not exist).")  # one answer for both
    eid = ctx.entity(a.get("entity_id"), f"the {svc} action", multiple=False)[0]
    if domain_of(eid) != domain_of(svc):
        raise DefinitionError("The entity must be in the same domain as the service.")
    if not ctx.writable(eid):
        raise DefinitionError(f"The entity {eid} is not on the write list set by the owner, so an automation may not change it.")
    data = a.get("data") if a.get("data") is not None else {}
    if has_template(data):
        raise DefinitionError("data contains a template, which is not allowed in an automation this connector writes.")
    problem = check_service_data(data, eid)
    if problem:
        raise DefinitionError(problem)
    out: dict[str, Any] = {"action": svc, "target": {"entity_id": eid}}
    if data:
        out["data"] = data
    return out, eid


def build_config(config_id: str, alias: Any, description: Any, mode: Any, triggers: Any, conditions: Any, actions: Any, ctx: Context) -> dict[str, Any]:
    """The Home Assistant automation config for these arguments, or DefinitionError. Nothing the caller sent is passed through unchecked."""
    alias = alias.strip() if isinstance(alias, str) else ""
    if not alias or len(alias) > 100 or re.search(r"[\x00-\x1f\x7f]", alias) or has_template(alias):
        raise DefinitionError("alias is a short name (up to 100 characters) with no control characters or templates.")
    description = description.strip() if isinstance(description, str) else ""
    if len(description) > 300 or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", description) or has_template(description):
        raise DefinitionError("description is up to 300 characters with no control characters or templates.")
    if mode not in ("single", "restart"):
        raise DefinitionError("mode is single or restart.")
    for name, value in (("triggers", triggers), ("conditions", conditions), ("actions", actions)):
        if value is not None and (not isinstance(value, list) or len(value) > LIMITS[name]):
            raise DefinitionError(f"{name} is a list of at most {LIMITS[name]} items.")
    if not triggers or not actions:
        raise DefinitionError("An automation needs at least one trigger and one action.")
    built_triggers, watched = [], set()
    for t in triggers:
        b, w = _trigger(t, ctx)
        built_triggers.append(b)
        watched.update(w)
    built_actions, changed = [], set()
    for a in actions:
        b, eid = _action(a, ctx)
        built_actions.append(b)
        if eid:
            changed.add(eid)
    if watched & changed:
        raise DefinitionError("An action changes an entity that one of its own triggers watches, which could make it trigger itself over and over.")
    if not changed:
        raise DefinitionError("An automation needs at least one service action, not only delays.")
    config: dict[str, Any] = {"id": config_id, "alias": alias}
    if description:
        config["description"] = description
    config.update({"mode": mode, "triggers": built_triggers})
    if conditions:
        config["conditions"] = [_condition(c, ctx) for c in conditions]
    config["actions"] = built_actions
    return config


def definition_problem(config: Any, hidden: Callable[[str], bool]) -> str | None:
    """Why an existing definition may not be shown (None when it is fine): templates, or text naming something the policy hides."""
    if has_template(config):
        return "templates"
    for text in _all_strings(config):
        if hidden(text):
            return "hidden"
    return None


def _all_strings(value: Any, depth: int = 0):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)) and depth < 8:
        for v in value:
            yield from _all_strings(v, depth + 1)
    elif isinstance(value, dict) and depth < 8:
        for k, v in value.items():
            yield from _all_strings(k, depth + 1)
            yield from _all_strings(v, depth + 1)

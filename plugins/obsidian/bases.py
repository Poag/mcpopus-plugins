"""Obsidian Bases (.base) support: safe YAML parsing, a short summary, and links to bases. Design: docs/DESIGN-bases.md.

Everything here works on text that came from the vault, so it is treated as hostile: the parser refuses anchors, aliases
and object-constructing tags, bounds the size of the structure before building it, and every value that is shown is clipped.
The YAML library is imported when needed. The host image may not have it; then the raw text is still returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .livesync import nfc
from .parse import mask_code

MAX_NODES = 20_000
MAX_DEPTH = 24
MAX_SUMMARY_VIEWS = 30
MAX_SUMMARY_NAMES = 40


@dataclass
class ParseResult:
    status: str  # ok | failed | refused | unavailable
    data: Any = None
    reason: str = ""


class _Refused(Exception):
    pass


def _check_tree(node: Any, yaml: Any) -> None:
    """Bound the document before any Python object is built from it. A node reached twice is an alias."""
    seen: set[int] = set()
    stack: list[tuple[Any, int]] = [(node, 1)]
    while stack:
        n, depth = stack.pop()
        if id(n) in seen:
            raise _Refused("anchors and aliases are not accepted")
        seen.add(id(n))
        if len(seen) > MAX_NODES:
            raise _Refused("the document has too many entries")
        if depth > MAX_DEPTH:
            raise _Refused("the document is nested too deeply")
        if isinstance(n, yaml.SequenceNode):
            stack.extend((c, depth + 1) for c in n.value)
        elif isinstance(n, yaml.MappingNode):
            for k, v in n.value:
                stack.append((k, depth + 1))
                stack.append((v, depth + 1))


def parse_base(text: str) -> ParseResult:
    """Parse a .base file with PyYAML's SafeLoader. Never raises; the reason never repeats the text."""
    try:
        import yaml
    except ImportError:
        return ParseResult("unavailable", reason="PyYAML is missing: install the plugin's packages from its page; only the raw text is shown")
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return ParseResult("ok", {})
        _check_tree(node, yaml)
        data = loader.construct_document(node)
    except _Refused as e:
        return ParseResult("refused", reason=str(e))
    except yaml.constructor.ConstructorError:
        return ParseResult("refused", reason="the document uses a YAML tag that is not accepted")
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark is not None else ""
        return ParseResult("failed", reason=f"not valid YAML{where}")
    except (RecursionError, ValueError, OverflowError, TypeError):
        return ParseResult("failed", reason="not valid YAML")
    finally:
        loader.dispose()
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return ParseResult("failed", reason="the top level of a base must be a mapping")
    return ParseResult("ok", data)


# ------------------------------------------------------------ write checks
class BaseInvalid(ValueError):
    """The text is not a base this plugin will store. The message names the problem and never quotes the text."""


WRITE_TOP_KEYS = ("filters", "formulas", "properties", "summaries", "views")
WRITE_VIEW_TYPES = ("table", "list", "cards")
WRITE_VIEW_KEYS = ("type", "name", "filters", "order", "sort", "limit", "groupBy", "groupOrder", "summaries")
MAX_WRITE_VIEWS = 30
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def normalise_base_text(text: str) -> tuple[str, bool]:
    """Text as it will be stored: valid characters only, LF line ends, one final newline. Returns (text, changed)."""
    if not text or not text.strip():
        raise BaseInvalid("the base is empty")
    if text.startswith("\ufeff"):
        raise BaseInvalid("the text starts with a byte order mark")
    out = text.replace("\r\n", "\n")
    if _CONTROL.search(out) or "\r" in out:
        raise BaseInvalid("the text contains control characters")
    if not out.endswith("\n"):
        out += "\n"
    return out, out != text


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or _CONTROL.search(value):
        raise BaseInvalid(f"{what} must be text of 1 to {limit} characters without control characters")
    return value


def _check_filter(node: Any, what: str, depth: int = 0) -> None:
    if depth > 8:
        raise BaseInvalid(f"{what} is nested too deeply")
    if isinstance(node, str):
        _text(node, what, 1000)
        return
    if isinstance(node, dict) and len(node) == 1 and next(iter(node)) in ("and", "or", "not"):
        children = next(iter(node.values()))
        if not isinstance(children, list) or not children or len(children) > 100:
            raise BaseInvalid(f"{what}: and, or and not need a list of 1 to 100 entries")
        for c in children:
            _check_filter(c, what, depth + 1)
        return
    raise BaseInvalid(f"{what} must be text, or one of and, or, not holding a list")


def _direction(value: Any, what: str) -> None:
    if value not in ("ASC", "DESC"):
        raise BaseInvalid(f"{what} direction must be ASC or DESC")


def validate_base_structure(data: Any) -> None:
    """Raise BaseInvalid unless the parsed base uses only the structure verified in docs/DESIGN-bases.md (12.4). Expressions are not parsed."""
    if not isinstance(data, dict):
        raise BaseInvalid("the top level must be a mapping")
    for key in data:
        if key not in WRITE_TOP_KEYS:
            raise BaseInvalid("a top-level key is not one of filters, formulas, properties, summaries, views")
    if "filters" in data:
        _check_filter(data["filters"], "the global filters")
    for section in ("formulas", "summaries"):
        if section in data:
            block = data[section]
            if not isinstance(block, dict) or len(block) > 200:
                raise BaseInvalid(f"{section} must be a mapping of up to 200 entries")
            for k, v in block.items():
                _text(k, f"a {section} name", 100)
                _text(v, f"a {section} entry", 1000)
    if "properties" in data:
        block = data["properties"]
        if not isinstance(block, dict) or len(block) > 200:
            raise BaseInvalid("properties must be a mapping of up to 200 entries")
        for k, v in block.items():
            _text(k, "a property name", 200)
            if not isinstance(v, dict) or set(v) - {"displayName"}:
                raise BaseInvalid("a property may only have displayName")
            if "displayName" in v:
                _text(v["displayName"], "displayName", 200)
    views = data.get("views")
    if not isinstance(views, list) or not views or len(views) > MAX_WRITE_VIEWS:
        raise BaseInvalid(f"views must be a list of 1 to {MAX_WRITE_VIEWS} views")
    for i, view in enumerate(views, 1):
        n = f"view {i}"
        if not isinstance(view, dict):
            raise BaseInvalid(f"{n} must be a mapping")
        for key in view:
            if key not in WRITE_VIEW_KEYS:
                raise BaseInvalid(f"{n} has a key that is not one of {', '.join(WRITE_VIEW_KEYS)}")
        if view.get("type") not in WRITE_VIEW_TYPES:
            raise BaseInvalid(f"{n} type must be one of {', '.join(WRITE_VIEW_TYPES)}")
        if "name" in view:
            _text(view["name"], f"{n} name", 100)
        if "limit" in view and (isinstance(view["limit"], bool) or not isinstance(view["limit"], int) or not 1 <= view["limit"] <= 10_000):
            raise BaseInvalid(f"{n} limit must be a whole number from 1 to 10000")
        if "filters" in view:
            _check_filter(view["filters"], f"{n} filters")
        if "order" in view:
            if not isinstance(view["order"], list) or len(view["order"]) > 200:
                raise BaseInvalid(f"{n} order must be a list of up to 200 property names")
            for c in view["order"]:
                _text(c, f"{n} order entry", 200)
        if "sort" in view:
            if not isinstance(view["sort"], list) or len(view["sort"]) > 20:
                raise BaseInvalid(f"{n} sort must be a list of up to 20 entries")
            for s in view["sort"]:
                if not isinstance(s, dict) or set(s) != {"property", "direction"}:
                    raise BaseInvalid(f"{n} sort entries need exactly property and direction")
                _text(s["property"], f"{n} sort property", 200)
                _direction(s["direction"], f"{n} sort")
        if "groupBy" in view:
            g = view["groupBy"]
            if not isinstance(g, dict) or set(g) != {"property", "direction"}:
                raise BaseInvalid(f"{n} groupBy needs exactly property and direction")
            _text(g["property"], f"{n} groupBy property", 200)
            _direction(g["direction"], f"{n} groupBy")
        if "groupOrder" in view:
            if not isinstance(view["groupOrder"], list) or len(view["groupOrder"]) > 200:
                raise BaseInvalid(f"{n} groupOrder must be a list of up to 200 texts")
            for c in view["groupOrder"]:
                if not isinstance(c, str) or len(c) > 200 or _CONTROL.search(c):
                    raise BaseInvalid(f"{n} groupOrder entries must be short text")
        if "summaries" in view:
            sm = view["summaries"]
            if not isinstance(sm, dict) or len(sm) > 200:
                raise BaseInvalid(f"{n} summaries must be a mapping of up to 200 entries")
            for k, v in sm.items():
                _text(k, f"{n} summaries property", 200)
                _text(v, f"{n} summaries entry", 200)


_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.S)
MAX_FRONTMATTER_CHARS = 65_536


def frontmatter_props(text: str) -> dict | None:
    """The properties at the top of a note: {} if it has none, None if they cannot be read (not valid YAML, too large, refused by the guards)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}
    block = m.group(1) or ""
    if len(block) > MAX_FRONTMATTER_CHARS:
        return None
    result = parse_base(block)
    return result.data if result.status == "ok" else None


# --------------------------------------------------------------------- summary
def clip(value: Any, limit: int = 200) -> str:
    s = re.sub(r"\s+", " ", str(value)).strip()
    s = "".join(ch for ch in s if ch.isprintable())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def render_filter(node: Any, depth: int = 0) -> str:
    if depth > 8:
        return "…"
    if isinstance(node, str):
        return clip(node, 300)
    if isinstance(node, dict) and len(node) == 1:
        op, children = next(iter(node.items()))
        if op in ("and", "or", "not") and isinstance(children, list):
            return f"{op}(" + "; ".join(render_filter(c, depth + 1) for c in children[:20]) + (f"; and {len(children) - 20} more" if len(children) > 20 else "") + ")"
    return "(not in a form this summary understands)"


def _names(d: Any) -> list[str]:
    return [clip(k, 80) for k in list(d)[:MAX_SUMMARY_NAMES]] if isinstance(d, dict) else []


def summarise(data: dict) -> list[str]:
    """A few lines about the structure of a parsed base. Values are clipped; nothing is evaluated."""
    lines = ["top-level keys: " + (", ".join(clip(k, 60) for k in list(data)[:20]) or "(none)")]
    if "filters" in data:
        lines.append("filters (all views): " + render_filter(data["filters"]))
    formulas = data.get("formulas")
    if isinstance(formulas, dict) and formulas:
        lines.append(f"formulas ({len(formulas)}):")
        lines.extend(f"  {clip(k, 80)}: {clip(v, 200)}" for k, v in list(formulas.items())[:MAX_SUMMARY_NAMES])
    props = data.get("properties")
    if isinstance(props, dict) and props:
        lines.append("properties with settings: " + ", ".join(_names(props)))
    sums = data.get("summaries")
    if isinstance(sums, dict) and sums:
        lines.append("custom summaries: " + ", ".join(_names(sums)))
    views = data.get("views")
    if isinstance(views, list):
        lines.append(f"views ({len(views)}):")
        for i, v in enumerate(views[:MAX_SUMMARY_VIEWS], 1):
            if not isinstance(v, dict):
                lines.append(f"  {i}. (not a mapping)")
                continue
            bits = [clip(v.get("type", "?"), 30), repr(clip(v.get("name", ""), 80))]
            if "limit" in v:
                bits.append(f"limit {clip(v['limit'], 20)}")
            gb = v.get("groupBy")
            if isinstance(gb, dict):
                bits.append(f"group by {clip(gb.get('property', '?'), 80)} {clip(gb.get('direction', ''), 10)}".strip())
            if "filters" in v:
                bits.append("filters: " + render_filter(v["filters"]))
            order = v.get("order")
            if isinstance(order, list):
                bits.append("columns: " + ", ".join(clip(c, 60) for c in order[:20]))
            extra = [clip(k, 40) for k in v if k not in ("type", "name", "limit", "groupBy", "filters", "order")]
            if extra:
                bits.append("other keys: " + ", ".join(extra[:15]))
            lines.append(f"  {i}. " + " | ".join(bits))
        if len(views) > MAX_SUMMARY_VIEWS:
            lines.append(f"  and {len(views) - MAX_SUMMARY_VIEWS} more")
    elif views is not None:
        lines.append("views: (not a list)")
    return lines


# ----------------------------------------------------------------------- links
_BASE_WIKI = re.compile(r"\[\[([^\]|#\n]+?\.base)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]", re.IGNORECASE)
_BASE_MD = re.compile(r"\[[^\]\n]*\]\(<?([^)\n>]+?\.base)(?:#[^)\n>]*)?>?\)", re.IGNORECASE)


def base_links(content: str) -> list[str]:
    """Targets of links and embeds that point at a .base file: [[x.base]], ![[x.base#View]], [t](x.base). Code is ignored."""
    masked = mask_code(content)
    found = [m.group(1) for m in _BASE_WIKI.finditer(masked)]
    found += [m.group(1).replace("%20", " ") for m in _BASE_MD.finditer(masked)]
    return list(dict.fromkeys(t.strip().lstrip("/") for t in found if t.strip()))


def link_matches_base(target: str, base_path: str) -> bool:
    """Does a link target point at this base? Case-insensitive, NFC, whole path or a path suffix (the rule used for notes)."""
    a, b = nfc(target).strip().lstrip("/").casefold(), nfc(base_path).casefold()
    return bool(a) and (b == a or b.endswith("/" + a))

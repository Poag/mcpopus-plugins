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

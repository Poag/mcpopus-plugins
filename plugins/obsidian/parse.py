"""Frontmatter, tags and links, following es617/obsidian-sync-mcp's parse.ts (MIT) so results match what Claude is used to."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_FM_KEY = re.compile(r"^([\w][\w-]*)\s*:\s*(.+)")
_TAG = re.compile(r"(^|\s)#([\w/-][\w/-]*)", re.UNICODE)
_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_MDLINK = re.compile(r"\[([^\]]+)\]\(([^)]+\.md)\)")


@dataclass
class Parsed:
    frontmatter: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def mask_code(content: str) -> str:
    """Blank out fenced and inline code so '#' inside it is not a tag."""
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for line in content.split("\n"):
        m = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            out.append("." * len(line))
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and line.strip() == m.group(1):
                fence = None
            continue
        if m and (m.group(1)[0] == "~" or "`" not in line[m.end():]):
            fence = (m.group(1)[0], len(m.group(1)))
            out.append("." * len(line))
            continue
        out.append(re.sub(r"(`+)(.+?)\1", lambda mm: "." * len(mm.group(0)), line))
    return "\n".join(out)


def parse(content: str) -> Parsed:
    tags: dict[str, None] = {}
    fm: dict[str, str] = {}
    if content.startswith("---\n"):
        end = content.find("\n---", 4)
        if end != -1:
            in_list = False
            for line in content[4:end].split("\n"):
                if in_list:
                    item = re.match(r"^\s+-\s+(.+)", line)
                    if item:
                        if item.group(1).strip():
                            tags[item.group(1).strip()] = None
                        continue
                    in_list = False
                m = _FM_KEY.match(line)
                if m:
                    fm[m.group(1)] = m.group(2).strip()
                if re.match(r"^tags\s*:", line):
                    value = re.sub(r"^tags\s*:\s*", "", line).strip()
                    if value:
                        for t in re.sub(r"[\[\]]", "", value).split(","):
                            if t.strip():
                                tags[t.strip()] = None
                    else:
                        in_list = True
    for m in _TAG.finditer(mask_code(content)):
        if not m.group(2).isdigit():
            tags[m.group(2)] = None
    links = [m.group(1) for m in _WIKI.finditer(content)] + [m.group(2) for m in _MDLINK.finditer(content)]
    return Parsed(fm, list(tags), list(dict.fromkeys(links)))


def snippet(content: str, query: str, context: int = 80) -> str:
    low, q = content.lower(), query.lower()
    idx = low.find(q)
    if idx == -1:
        for w in (w for w in re.split(r"\s+", q) if len(w) >= 3):
            idx = low.find(w)
            if idx != -1:
                q = w
                break
    if idx == -1:
        return content[: context * 2].replace("\n", " ")
    start, end = max(0, idx - context), min(len(content), idx + len(q) + context)
    return ("…" if start else "") + content[start:end].replace("\n", " ") + ("…" if end < len(content) else "")


def link_target_matches(link: str, note_path: str) -> bool:
    """Does [[link]] or a markdown link point at this note? Case-insensitive, extension optional, path suffix allowed."""
    a = link.strip().split("#")[0].removesuffix(".md").casefold()
    b = note_path.removesuffix(".md").casefold()
    return bool(a) and (b == a or b.endswith("/" + a))

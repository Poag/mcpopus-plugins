"""query_base: work out which notes a base's view would list, from this plugin's own index. Design: docs/DESIGN-bases.md section 11.

Only constructs that are documented or seen in a real .base file are evaluated. Anything else raises Unsupported, naming the construct, and the tool
returns no rows. Nothing here runs `eval`: expressions go through the small parser below. Everything that comes from a base or a note is hostile text:
error messages quote identifiers only (letters, digits, underscore, dot), clipped.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from .livesync import nfc

UTC = dt.timezone.utc
MAX_EXPR_CHARS = 1000
MAX_NODES = 300
MAX_DEPTH = 30
MAX_ARGS = 20
MAX_FILTER_NODES = 200
STEP_BUDGET = 3_000_000
MAX_CELL = 100

SUPPORTED_LINE = (
    "Supported: table views; filters (and, or, not); file.name, basename, path, folder, ext, size, ctime, mtime; note properties; "
    "file.hasTag, inFolder, hasLink, hasProperty; now(), today(), date(); == != < > <= >=, && || !, + - * / %; "
    "contains, containsAll, containsAny, startsWith, endsWith, isEmpty, lower, trim, length; sort; limit. Times are UTC."
)

FILE_PROPS = ("name", "basename", "path", "folder", "ext", "size", "ctime", "mtime")
COLUMN_FILE_PROPS = FILE_PROPS + ("tags",)
FILE_CALLS = {"hasTag": (1, MAX_ARGS), "inFolder": (1, 1), "hasLink": (1, 1), "hasProperty": (1, 1)}
GLOBAL_CALLS = {"now": (0, 0), "today": (0, 0), "date": (1, 1)}
METHODS = {"contains": (1, 1), "containsAll": (1, MAX_ARGS), "containsAny": (1, MAX_ARGS), "startsWith": (1, 1), "endsWith": (1, 1),
           "isEmpty": (0, 0), "lower": (0, 0), "trim": (0, 0), "date": (0, 0)}
FIELDS = ("length", "year", "month", "day", "hour", "minute", "second")
VIEW_KEYS = ("type", "name", "filters", "order", "sort", "limit")
TOP_KEYS = ("filters", "formulas", "properties", "summaries", "views")
GROUPING_KEYS = ("groupBy", "groupOrder", "summaries")


class Unsupported(Exception):
    """A construct this tool does not evaluate. The message names it and is safe to show."""


class Mismatch(Exception):
    """A value of the wrong type for an operation. The note is treated as not matching and counted."""


class Budget(Exception):
    """The query needs more work than allowed."""


class NoSuchView(Exception):
    pass


def ident(text: Any, limit: int = 40) -> str:
    return re.sub(r"[^\w.\-]", "?", str(text))[:limit]


# ------------------------------------------------------------------- tokens
_TOKEN = re.compile(
    r"""\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(?P<id>[^\W\d]\w*)|(?P<op>==|!=|>=|<=|&&|\|\||[()<>!+\-*/%.,\[\]]))"""
)
_ESC = {"n": "\n", "t": "\t", "r": "\r"}


def tokenize(text: str) -> list[tuple[str, Any]]:
    if len(text) > MAX_EXPR_CHARS:
        raise Unsupported("an expression longer than 1000 characters")
    out: list[tuple[str, Any]] = []
    pos = 0
    while True:
        m = _TOKEN.match(text, pos)
        if not m:
            rest = text[pos:].strip()
            if rest:
                raise Unsupported(f'the character or operator "{ident(rest[:3], 3)}"')
            return out
        pos = m.end()
        if m.group("num") is not None:
            raw = m.group("num")
            out.append(("num", float(raw) if "." in raw else int(raw)))
        elif m.group("str") is not None:
            body = m.group("str")[1:-1]
            out.append(("str", re.sub(r"\\(.)", lambda mm: _ESC.get(mm.group(1), mm.group(1)), body)))
        elif m.group("id") is not None:
            out.append(("id", m.group("id")))
        else:
            out.append(("op", m.group("op")))


# ------------------------------------------------------------------- parser
@dataclass
class Uses:
    """What an expression needs from the index."""
    text: bool = False  # tags, links or properties (needs note contents)
    props: bool = False  # frontmatter
    time: bool = False


_LEVELS = (("||",), ("&&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%"))


class Parser:
    def __init__(self, text: str, uses: Uses) -> None:
        self.toks = tokenize(text)
        self.i = 0
        self.nodes = 0
        self.uses = uses

    # -- helpers
    def peek(self, k: int = 0) -> tuple[str, Any]:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else ("end", None)

    def take(self) -> tuple[str, Any]:
        tok = self.peek()
        self.i += 1
        return tok

    def is_op(self, value: str, k: int = 0) -> bool:
        return self.peek(k) == ("op", value)

    def expect_op(self, value: str) -> None:
        if not self.is_op(value):
            raise Unsupported("an expression this tool cannot read")
        self.i += 1

    def node(self, *parts: Any) -> tuple:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise Unsupported("an expression with more than 300 parts")
        return parts

    # -- grammar
    def parse(self) -> tuple:
        node = self.binary(0, 0)
        if self.i != len(self.toks):
            raise Unsupported("an expression this tool cannot read")
        return node

    def binary(self, level: int, depth: int) -> tuple:
        if depth > MAX_DEPTH:
            raise Unsupported("an expression nested more than 30 levels")
        if level == len(_LEVELS):
            return self.unary(depth)
        left = self.binary(level + 1, depth)
        while self.peek()[0] == "op" and self.peek()[1] in _LEVELS[level]:
            op = self.take()[1]
            right = self.binary(level + 1, depth)
            left = self.node("bin", op, left, right)
        return left

    def unary(self, depth: int) -> tuple:
        if depth > MAX_DEPTH:
            raise Unsupported("an expression nested more than 30 levels")
        if self.is_op("!") or self.is_op("-"):
            op = self.take()[1]
            return self.node("un", op, self.unary(depth + 1))
        return self.postfix(self.primary(depth), depth)

    def args(self, low: int, high: int, depth: int, name: str) -> list[tuple]:
        self.expect_op("(")
        found: list[tuple] = []
        if not self.is_op(")"):
            while True:
                found.append(self.binary(0, depth + 1))
                if self.is_op(","):
                    self.i += 1
                    continue
                break
        self.expect_op(")")
        if not low <= len(found) <= high:
            raise Unsupported(f"the wrong number of arguments to {ident(name)}()")
        return found

    def postfix(self, node: tuple, depth: int) -> tuple:
        while self.is_op("."):
            self.i += 1
            kind, name = self.take()
            if kind != "id":
                raise Unsupported("an expression this tool cannot read")
            if self.is_op("("):
                if name not in METHODS:
                    raise Unsupported(f"the method .{ident(name)}()")
                low, high = METHODS[name]
                node = self.node("method", node, name, self.args(low, high, depth, name))
            else:
                if name not in FIELDS:
                    raise Unsupported(f"the field .{ident(name)}")
                node = self.node("field", node, name)
        if self.is_op("["):
            raise Unsupported("indexing a value with [ ]")
        return node

    def primary(self, depth: int) -> tuple:
        kind, value = self.take()
        if kind in ("num", "str"):
            return self.node("lit", value)
        if kind == "op" and value == "(":
            inner = self.binary(0, depth + 1)
            self.expect_op(")")
            return inner
        if kind != "id":
            raise Unsupported("an expression this tool cannot read")
        if value in ("true", "false"):
            return self.node("lit", value == "true")
        if value == "null":
            return self.node("lit", None)
        if value == "this":
            raise Unsupported("this (the file the base is shown in)")
        if value == "formula":
            raise Unsupported("formulas (formula.x)")
        if value == "file":
            return self.file_ref(depth)
        if value == "note":
            if self.is_op("."):
                self.i += 1
                k, name = self.take()
                if k != "id":
                    raise Unsupported("an expression this tool cannot read")
            elif self.is_op("["):
                self.i += 1
                k, name = self.take()
                if k != "str":
                    raise Unsupported("indexing a value with [ ]")
                self.expect_op("]")
            else:
                raise Unsupported("the note object")
            self.uses.text = self.uses.props = True
            return self.node("prop", "note", name)
        if self.is_op("("):
            if value not in GLOBAL_CALLS:
                raise Unsupported(f"the function {ident(value)}()")
            low, high = GLOBAL_CALLS[value]
            args = self.args(low, high, depth, value)
            if value == "date" and args[0][0] == "lit":
                if not isinstance(args[0][1], str) or parse_date_text(args[0][1]) is None:
                    raise Unsupported("date() with text that is not YYYY-MM-DD or YYYY-MM-DD HH:mm:ss")
            if value in ("now", "today"):
                self.uses.time = True
            return self.node("call", value, args)
        self.uses.text = self.uses.props = True
        return self.node("prop", "note", value)

    def file_ref(self, depth: int) -> tuple:
        if not self.is_op("."):
            raise Unsupported("the file object")
        self.i += 1
        kind, name = self.take()
        if kind != "id":
            raise Unsupported("an expression this tool cannot read")
        if self.is_op("(") and name in FILE_CALLS:
            low, high = FILE_CALLS[name]
            if name != "inFolder":  # the folder is known from the path; tags, links and properties need the note's contents
                self.uses.text = True
            if name == "hasProperty":
                self.uses.props = True
            return self.node("filecall", name, self.args(low, high, depth, name))
        if name in FILE_PROPS:
            return self.node("prop", "file", name)
        if name == "tags":
            raise Unsupported("file.tags in a filter (use file.hasTag)")
        raise Unsupported(f"file.{ident(name)}")


# ------------------------------------------------------------------ values
@dataclass
class FileRec:
    path: str
    is_note: bool
    size: int
    ctime: dt.datetime | None
    mtime: dt.datetime | None
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    props: dict | None = field(default_factory=dict)  # None: frontmatter could not be read
    readable: bool = True  # False: contents could not be read

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def basename(self) -> str:
        n = self.name
        return n.rsplit(".", 1)[0] if "." in n[1:] else n

    @property
    def ext(self) -> str:
        n = self.name
        return n.rsplit(".", 1)[1] if "." in n[1:] else ""

    @property
    def folder(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""


def kind_of(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, str):
        return "str"
    if isinstance(v, dt.datetime):
        return "date"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "obj"
    return "other"


def norm_value(v: Any) -> Any:
    """A frontmatter value as the evaluator sees it: dates become aware UTC datetimes."""
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day, tzinfo=UTC)
    return v


def parse_date_text(text: str) -> dt.datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


_DUR = re.compile(r"^\s*(\d{1,6})\s*([A-Za-z]+)\s*$")
_UNITS = {"s": 1, "second": 1, "seconds": 1, "m": 60, "minute": 60, "minutes": 60, "h": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400, "w": 604800, "week": 604800, "weeks": 604800}
_CALENDAR = {"y", "year", "years", "M", "month", "months"}


def duration(text: str, literal: bool) -> dt.timedelta:
    m = _DUR.match(text)
    bad = Unsupported("a duration text that is not a number and a unit (seconds, minutes, hours, days or weeks)") if literal else Mismatch()
    if not m:
        raise bad
    unit = m.group(2)
    if unit in _CALENDAR:
        raise Unsupported("year and month durations (calendar arithmetic)")
    if unit not in _UNITS:
        raise bad
    return dt.timedelta(seconds=int(m.group(1)) * _UNITS[unit])


def truth(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    raise Mismatch()


def values_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    ka, kb = kind_of(a), kind_of(b)
    if ka != kb or ka not in ("bool", "num", "str", "date"):
        raise Mismatch()
    return a == b


def compare(op: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    ka, kb = kind_of(a), kind_of(b)
    if ka != kb or ka not in ("num", "str", "date"):
        raise Mismatch()
    return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]


def arith(op: str, a: Any, b: Any, right_literal: bool) -> Any:
    if a is None or b is None:
        raise Mismatch()
    ka, kb = kind_of(a), kind_of(b)
    if op == "+" and ka == kb == "str":
        return a + b
    if ka == kb == "num":
        if op in ("/", "%") and b == 0:
            raise Mismatch()
        return {"+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b, "/": lambda: a / b, "%": lambda: a % b}[op]()
    if ka == "date" and kb == "str" and op in ("+", "-"):
        delta = duration(b, right_literal)
        return a + delta if op == "+" else a - delta
    if ka == kb == "date" and op == "-":
        raise Unsupported("subtracting one date from another")
    raise Mismatch()


def has_tag(rec: FileRec, names: list[Any]) -> bool:
    if not rec.is_note:
        return False
    have = {nfc(t).lstrip("#").casefold() for t in rec.tags}
    for n in names:
        if not isinstance(n, str):
            raise Mismatch()
        want = nfc(n).lstrip("#").casefold()
        if want and any(t == want or t.startswith(want + "/") for t in have):
            return True
    return False


def link_key(text: str) -> str:
    t = nfc(text).strip().split("#")[0].split("|")[0].strip().lstrip("/").casefold()
    return t[:-3] if t.endswith(".md") else t


def has_link(rec: FileRec, target: Any) -> bool:
    if not isinstance(target, str):
        raise Mismatch()
    a = link_key(target)
    if not a:
        raise Mismatch()
    for link in rec.links:
        b = link_key(link)
        if b and (b == a or b.endswith("/" + a) or a.endswith("/" + b)):
            return True
    return False


def in_folder(rec: FileRec, folder: Any) -> bool:
    if not isinstance(folder, str):
        raise Mismatch()
    f = nfc(folder).strip().strip("/").casefold()
    if not f:
        raise Mismatch()
    here = nfc(rec.folder).casefold()
    return here == f or here.startswith(f + "/")


def method(obj: Any, name: str, args: list[Any]) -> Any:
    k = kind_of(obj) if obj is not None else "null"
    if name == "isEmpty":
        if k == "null":
            return True
        if k in ("str", "list", "obj"):
            return len(obj) == 0
        if k in ("num", "date"):
            return False
        raise Mismatch()
    if k == "null":
        return None if name in ("lower", "trim", "date") else False
    if name == "date":
        if k != "date":
            raise Mismatch()
        return obj.replace(hour=0, minute=0, second=0, microsecond=0)
    if k == "str":
        if name in ("lower", "trim"):
            return obj.lower() if name == "lower" else obj.strip()
        if not all(isinstance(a, str) for a in args):
            raise Mismatch()
        return {"contains": lambda: args[0] in obj, "containsAll": lambda: all(a in obj for a in args), "containsAny": lambda: any(a in obj for a in args),
                "startsWith": lambda: obj.startswith(args[0]), "endsWith": lambda: obj.endswith(args[0])}[name]()
    if k == "list":
        def has(x: Any) -> bool:
            try:
                return any(values_equal(item, x) for item in obj)
            except Mismatch:
                return any(kind_of(item) == kind_of(x) and values_equal(item, x) for item in obj if kind_of(item) in ("bool", "num", "str", "date"))
        if name == "contains":
            return has(args[0])
        if name == "containsAll":
            return all(has(a) for a in args)
        if name == "containsAny":
            return any(has(a) for a in args)
    raise Mismatch()


# ---------------------------------------------------------------- evaluation
@dataclass
class Ctx:
    now: dt.datetime
    steps: int = 0


def file_value(rec: FileRec, name: str) -> Any:
    if name == "tags":
        return list(rec.tags)
    # Verified live 10 Nov 2026: file.name has no extension (a base filtering file.name == "zzqx" listed zzqx.md)
    return {"name": rec.basename, "basename": rec.basename, "path": rec.path, "folder": rec.folder, "ext": rec.ext,
            "size": rec.size, "ctime": rec.ctime, "mtime": rec.mtime}[name]


def note_value(rec: FileRec, name: str) -> Any:
    if not rec.is_note or rec.props is None:
        return None
    return norm_value(rec.props.get(name))


def ev(node: tuple, rec: FileRec, ctx: Ctx) -> Any:
    ctx.steps += 1
    if ctx.steps > STEP_BUDGET:
        raise Budget()
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "prop":
        return file_value(rec, node[2]) if node[1] == "file" else note_value(rec, node[2])
    if kind == "un":
        v = ev(node[2], rec, ctx)
        if node[1] == "!":
            return not truth(v)
        if kind_of(v) != "num":
            raise Mismatch()
        return -v
    if kind == "bin":
        op = node[1]
        if op == "&&":
            return truth(ev(node[2], rec, ctx)) and truth(ev(node[3], rec, ctx))
        if op == "||":
            return truth(ev(node[2], rec, ctx)) or truth(ev(node[3], rec, ctx))
        a, b = ev(node[2], rec, ctx), ev(node[3], rec, ctx)
        if op == "==":
            return values_equal(a, b)
        if op == "!=":
            return not values_equal(a, b)
        if op in ("<", ">", "<=", ">="):
            return compare(op, a, b)
        return arith(op, a, b, node[3][0] == "lit")
    if kind == "call":
        name = node[1]
        if name == "now":
            return ctx.now
        if name == "today":
            return ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
        arg = ev(node[2][0], rec, ctx)
        if not isinstance(arg, str):
            raise Mismatch()
        parsed = parse_date_text(arg)
        if parsed is None:
            raise Mismatch()
        return parsed
    if kind == "filecall":
        args = [ev(a, rec, ctx) for a in node[2]]
        name = node[1]
        if name == "hasTag":
            return has_tag(rec, args)
        if name == "inFolder":
            return in_folder(rec, args[0])
        if name == "hasLink":
            return has_link(rec, args[0])
        if not isinstance(args[0], str):
            raise Mismatch()
        return rec.is_note and rec.props is not None and args[0] in rec.props
    if kind == "method":
        return method(ev(node[1], rec, ctx), node[2], [ev(a, rec, ctx) for a in node[3]])
    if kind == "field":
        obj, name = ev(node[1], rec, ctx), node[2]
        if obj is None:
            return None
        k = kind_of(obj)
        if name == "length" and k in ("str", "list"):
            return len(obj)
        if k == "date" and name != "length":
            return getattr(obj, name)
        raise Mismatch()
    raise Unsupported("an expression this tool cannot read")


# ------------------------------------------------------------------ filters
def compile_filter(obj: Any, uses: Uses, counter: list[int], depth: int = 0) -> tuple:
    counter[0] += 1
    if counter[0] > MAX_FILTER_NODES or depth > 12:
        raise Unsupported("a filter with more than 200 entries or nested more than 12 levels")
    if isinstance(obj, str):
        return ("stmt", Parser(obj, uses).parse())
    if isinstance(obj, dict):
        if len(obj) != 1 or next(iter(obj)) not in ("and", "or", "not"):
            raise Unsupported("a filter group that is not a single and, or or not")
        op, children = next(iter(obj.items()))
        if not isinstance(children, list) or not children:
            raise Unsupported("an empty or malformed filter group")
        return (op, [compile_filter(c, uses, counter, depth + 1) for c in children])
    raise Unsupported("a filter that is neither text nor a group")


def eval_filter(tree: tuple, rec: FileRec, ctx: Ctx) -> bool:
    op = tree[0]
    if op == "stmt":
        return truth(ev(tree[1], rec, ctx))
    if op == "and":
        return all(eval_filter(c, rec, ctx) for c in tree[1])
    if op == "or":
        return any(eval_filter(c, rec, ctx) for c in tree[1])
    return not any(eval_filter(c, rec, ctx) for c in tree[1])  # not: none of the entries is true


# --------------------------------------------------------------------- plan
@dataclass
class Col:
    label: str
    ns: str  # file | note
    name: str


@dataclass
class Plan:
    view_name: str
    filters: list[tuple]
    filter_texts: list[str]
    columns: list[Col]
    sort: list[tuple[Col, bool]]  # (column, descending)
    limit: int | None
    uses: Uses
    views_total: int


def column_ref(text: Any, uses: Uses, *, sortable: bool = False) -> Col:
    if not isinstance(text, str) or not text.strip() or len(text) > 200:
        raise Unsupported("a column or sort property that is not text")
    t = text.strip()
    if t.startswith("formula."):
        raise Unsupported("formulas (formula.x)")
    if t.startswith("file."):
        name = t[5:]
        if name not in COLUMN_FILE_PROPS:
            raise Unsupported(f"file.{ident(name)}")
        if sortable and name == "tags":
            raise Unsupported("sorting by file.tags")
        if name == "tags":
            uses.text = True
        return Col(t, "file", name)
    if t.startswith("note."):
        t = t[5:]
    if t in ("file", "this", "formula") or t.startswith("this."):
        raise Unsupported(ident(t))
    uses.text = uses.props = True
    return Col(text.strip(), "note", t)


def plan_base(data: dict, view_name: str = "") -> Plan:
    for key in data:
        if key not in TOP_KEYS:
            raise Unsupported(f'the top-level key "{ident(key)}"')
    views = data.get("views")
    if not isinstance(views, list) or not views or not all(isinstance(v, dict) for v in views):
        raise Unsupported("a base with no views, or views that are not mappings")
    if view_name:
        view = next((v for v in views if isinstance(v.get("name"), str) and v.get("name") == view_name), None)
        if view is None:
            names = ", ".join(repr(ident(v.get("name", ""), 60)) for v in views[:20])
            raise NoSuchView(f"No view with that name. Views in this base: {names}")
    else:
        view = views[0]
    for key in view:
        if key in GROUPING_KEYS:
            raise Unsupported(f'the view setting "{key}" (it changes how Obsidian arranges the rows)')
        if key not in VIEW_KEYS:
            raise Unsupported(f'the view setting "{ident(key)}" (not seen in a verified file; send a sample to have it added)')
    if view.get("type") != "table":
        raise Unsupported(f'the view type "{ident(view.get("type"))}" (only table views are supported)')
    uses = Uses()
    counter = [0]
    filters, texts = [], []
    for scope, obj in (("base", data.get("filters")), ("view", view.get("filters"))):
        if obj is not None:
            filters.append(compile_filter(obj, uses, counter))
            texts.append(f"{scope}: " + describe(obj))
    order = view.get("order", [])
    if not isinstance(order, list):
        raise Unsupported("a view order that is not a list")
    columns = [column_ref(c, uses) for c in order[:60]]
    if len(order) > 60:
        raise Unsupported("a view with more than 60 columns")
    sort_spec = view.get("sort", [])
    if not isinstance(sort_spec, list):
        raise Unsupported("a view sort that is not a list")
    sorts: list[tuple[Col, bool]] = []
    for entry in sort_spec[:10]:
        if not isinstance(entry, dict) or set(entry) - {"property", "direction"} or "property" not in entry:
            raise Unsupported("a sort entry that is not a property with a direction")
        direction = str(entry.get("direction", "ASC")).upper()
        if direction not in ("ASC", "DESC"):
            raise Unsupported("a sort direction that is not ASC or DESC")
        sorts.append((column_ref(entry["property"], uses, sortable=True), direction == "DESC"))
    if len(sort_spec) > 10:
        raise Unsupported("a view with more than 10 sort entries")
    limit = view.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise Unsupported("a view limit that is not a whole number")
    return Plan(str(view.get("name", "")), filters, texts, columns, sorts, limit, uses, len(views))


def describe(obj: Any, depth: int = 0) -> str:
    if isinstance(obj, str):
        return re.sub(r"\s+", " ", obj)[:200]
    if isinstance(obj, dict) and len(obj) == 1 and depth < 8:
        op, children = next(iter(obj.items()))
        if isinstance(children, list):
            return f"{ident(op, 4)}(" + "; ".join(describe(c, depth + 1) for c in children[:20]) + ")"
    return "?"


# ---------------------------------------------------------------------- run
@dataclass
class QueryResult:
    rows: list[FileRec]
    matched_notes: int
    other_matching: int
    type_problem_notes: int
    unreadable_notes: int
    bad_frontmatter_notes: int
    limited_to: int | None


def sort_key_value(col: Col, rec: FileRec) -> Any:
    v = file_value(rec, col.name) if col.ns == "file" else note_value(rec, col.name)
    return v


def _rank_key(v: Any) -> tuple:
    k = kind_of(v)
    if k == "num":
        return (0, v, "")
    if k == "str":
        return (1, 0, (v.casefold(), v))
    if k == "date":
        return (2, v.timestamp(), "")
    if k == "bool":
        return (3, int(v), "")
    return (4, 0, "")


def run_query(plan: Plan, records: list[FileRec], now: dt.datetime) -> QueryResult:
    ctx = Ctx(now)
    matched: list[FileRec] = []
    other = 0
    problems = 0
    for rec in records:
        try:
            ok = all(eval_filter(t, rec, ctx) for t in plan.filters)
        except Mismatch:
            ok = False
            if rec.is_note:
                problems += 1
        if ok:
            if rec.is_note:
                matched.append(rec)
            else:
                other += 1
    rows = sorted(matched, key=lambda r: (r.path.casefold(), r.path))
    for col, desc in reversed(plan.sort):
        have = [r for r in rows if sort_key_value(col, r) is not None]
        none = [r for r in rows if sort_key_value(col, r) is None]
        have.sort(key=lambda r: _rank_key(sort_key_value(col, r)), reverse=desc)
        rows = have + none
    total = len(rows)
    if plan.limit is not None:
        rows = rows[: plan.limit]
    notes = [r for r in records if r.is_note]
    return QueryResult(rows, total, other, problems, sum(1 for r in notes if not r.readable),
                       sum(1 for r in notes if r.readable and r.props is None), plan.limit if plan.limit is not None and plan.limit < total else None)


def cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M") + "Z"
    if isinstance(v, float):
        return f"{v:.10g}"
    if isinstance(v, list):
        s = ", ".join(cell(x) for x in v[:30])
    elif isinstance(v, dict):
        s = "{" + ", ".join(f"{ident(k, 30)}: {cell(x)}" for k, x in list(v.items())[:10]) + "}"
    else:
        s = str(v)
    s = "".join(ch if ch.isprintable() else " " for ch in re.sub(r"\s+", " ", s)).strip()
    return s if len(s) <= MAX_CELL else s[: MAX_CELL - 1] + "…"


def column_value(col: Col, rec: FileRec) -> Any:
    return file_value(rec, col.name) if col.ns == "file" else note_value(rec, col.name)

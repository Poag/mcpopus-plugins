"""Phase 3: write_base and edit_base (docs/DESIGN-bases.md section 12). Real host manager, real MCP client, fake CouchDB."""

import base64
import hashlib
import json

import pytest

from conftest import plugin_module, running_plugin
from livesync_writer import build_vault
from mcpopus.pluginconfig import Policy
from test_bases import base_docs
from test_write import E2EE_PASS, E2EE_SECRETS, SECRETS, call, run_e2ee, vals

writer = plugin_module("writer")
bases = plugin_module("bases")
BOTH = Policy(read_only=False, enable_tools=["write_base", "edit_base", "write_note", "edit_note", "move_note", "delete_note"])
GOOD = 'filters:\n  and:\n    - file.inFolder("Test")\nviews:\n  - type: table\n    name: Probe\n    order:\n      - file.name\n      - file.path\n    sort:\n      - property: file.name\n        direction: ASC\n    limit: 20\n'


async def run(couch, tmp, policy=BOTH, extra=None, **kw):
    couch.docs.clear()
    couch.changes.clear()
    couch.load(build_vault("plain"))
    for d in extra or []:
        couch.load(d)
    return running_plugin(tmp, "obsidian", vals(couch, **kw), SECRETS, policy=policy)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def stored_bytes(couch, doc_id, decode=lambda d: d):
    meta = couch.docs[doc_id]
    return b"".join(base64.b64decode(decode(couch.docs[c]["data"])) for c in meta["children"])


# ------------------------------------------------------------------ pure parts
def test_split_bytes_is_lossless_and_cuts_at_newlines():
    data = ("line of text\n" * 5000).encode() + "ü".encode() * 30000
    pieces = writer.split_bytes(data)
    assert b"".join(pieces) == data and all(0 < len(p) <= writer.CHUNK_BYTES for p in pieces) and len(pieces) > 3
    assert pieces[0].endswith(b"\n") and writer.split_bytes(b"") == [] and writer.split_bytes(b"abc") == [b"abc"]


def test_binary_documents_have_the_librarys_shape():
    data = b"views:\n  - type: table\n"
    chunks, meta = writer.build_binary("Scratch/X.base", data, "scratch/x.base", ctime=5, mtime=6)
    assert meta == {"_id": "scratch/x.base", "path": "Scratch/X.base", "ctime": 5, "mtime": 6, "size": len(data), "type": "newnote",
                    "children": [chunks[0]["_id"]], "eden": {}}
    assert chunks[0]["type"] == "leaf" and chunks[0]["data"] == base64.b64encode(data).decode()
    assert chunks[0]["_id"] == writer.chunk_id(chunks[0]["data"])  # the id hashes the stored base64 text


@pytest.mark.parametrize("text,fragment", [
    ("", "empty"), ("   \n", "empty"), ("﻿views: []", "byte order"), ("views:\x00", "control"), ("a\rb", "control"),
])
def test_text_rules(text, fragment):
    with pytest.raises(bases.BaseInvalid, match=fragment):
        bases.normalise_base_text(text)


def test_line_ends_are_normalised():
    assert bases.normalise_base_text("a\r\nb") == ("a\nb\n", True) and bases.normalise_base_text("a\n") == ("a\n", False)


def _v(**view):
    return {"views": [{"type": "table", **view}]}


VALID = [
    _v(), _v(name="x", limit=10, order=["file.name"], sort=[{"property": "a", "direction": "DESC"}]),
    _v(filters={"and": ['a == "b"', {"or": ["x", {"not": ["y"]}]}]}), _v(groupBy={"property": "p", "direction": "ASC"}, groupOrder=["a"], summaries={"p": "Average"}),
    {"filters": "file.ext == 'md'", "formulas": {"f": "a + b"}, "properties": {"status": {"displayName": "S"}}, "summaries": {"s": "values.mean()"}, "views": [{"type": "list"}, {"type": "cards"}]},
]
INVALID = [
    ([], "mapping"), ({}, "views"), ({"views": []}, "views"), ({"views": [], "other": 1}, "top-level"), ({"views": "x"}, "views"), ({"views": [1]}, "mapping"),
    ({"views": [{"type": "kanban"}]}, "type"), ({"views": [{"type": "map"}]}, "type"), ({"views": [{}]}, "type"), (_v(columnSize={"a": 1}), "key"),
    (_v(limit=0), "limit"), (_v(limit=True), "limit"), (_v(limit="5"), "limit"), (_v(limit=10_001), "limit"), (_v(name=""), "name"), (_v(name="x" * 101), "name"),
    (_v(filters=""), "filters"), (_v(filters=5), "filters"), (_v(filters={"and": []}), "list"), (_v(filters={"xor": ["a"]}), "filters"), (_v(filters={"and": ["a"], "or": ["b"]}), "filters"),
    (_v(filters={"and": [5]}), "filters"), (_v(filters="x" * 1001), "filters"), (_v(order="file.name"), "order"), (_v(order=[1]), "order"),
    (_v(sort=[{"property": "a"}]), "sort"), (_v(sort=[{"property": "a", "direction": "UP"}]), "ASC or DESC"), (_v(sort=[{"property": "a", "direction": "ASC", "x": 1}]), "sort"),
    (_v(groupBy={"property": "a"}), "groupBy"), (_v(groupBy={"property": "a", "direction": "asc"}), "ASC or DESC"), (_v(groupOrder="a"), "groupOrder"), (_v(summaries=[]), "summaries"),
    ({"properties": {"a": {"width": 3}}, "views": [{"type": "table"}]}, "displayName"), ({"formulas": {"f": 5}, "views": [{"type": "table"}]}, "formulas"),
    ({"formulas": [], "views": [{"type": "table"}]}, "formulas"), ({"views": [{"type": "table"}] * 31}, "views"),
]


def test_structure_accepts_the_verified_shapes():
    for data in VALID:
        bases.validate_base_structure(data)
    deep = "x"
    for _ in range(9):
        deep = {"and": [deep]}
    with pytest.raises(bases.BaseInvalid, match="deeply"):
        bases.validate_base_structure(_v(filters=deep))


@pytest.mark.parametrize("data,fragment", INVALID)
def test_structure_refuses_everything_else_by_name(data, fragment):
    with pytest.raises(bases.BaseInvalid, match=fragment):
        bases.validate_base_structure(data)


def test_the_error_never_quotes_the_text():
    with pytest.raises(bases.BaseInvalid) as e:
        bases.validate_base_structure({"secret-medical-token": 1, "views": [{"type": "table"}]})
    assert "secret-medical-token" not in str(e.value)


# ------------------------------------------------------------------ the tools
async def test_tools_need_both_switches_and_a_writable_folder(couch, tmp_path):
    async with await run(couch, tmp_path / "a", policy=None) as (c, _, _):
        assert not {"write_base", "edit_base"} & {t.name for t in (await c.list_tools()).tools}
    async with await run(couch, tmp_path / "b", writable_folders=[]) as (c, lp, _):
        assert {"write_base", "edit_base"} <= {t.name for t in (await c.list_tools()).tools}
        assert {m.name: m.tier for m in lp.declared if m.name in ("write_base", "edit_base")} == {"write_base": "write", "edit_base": "write"}
        err, text = await call(c, "write_base", {"path": "Scratch/A.base", "content": GOOD})
        assert err and "not enabled for that folder" in text
    assert couch.writes == []


async def test_create_has_the_exact_livesync_shape_and_reads_back(couch, tmp_path):
    async with await run(couch, tmp_path) as (c, _, _):
        err, text = await call(c, "write_base", {"path": "Scratch/Probe.base", "content": GOOD})
        assert not err and "Base created: Scratch/Probe.base" in text and "file=Scratch%2FProbe.base" in text
        data = GOOD.encode()
        assert f"sha256 before: none\nsha256 after: {sha(data)}" in text
        doc = couch.docs["scratch/probe.base"]
        assert set(doc) == {"_id", "_rev", "path", "ctime", "mtime", "size", "type", "children", "eden"}
        assert doc["type"] == "newnote" and doc["size"] == len(data) and doc["path"] == "Scratch/Probe.base" and doc["eden"] == {}
        ch = couch.docs[doc["children"][0]]
        assert ch["type"] == "leaf" and ch["_id"] == writer.chunk_id(ch["data"]) and "e_" not in ch and ch["data"] == base64.b64encode(data).decode()
        assert stored_bytes(couch, "scratch/probe.base") == data
        err, text = await call(c, "read_base", {"path": "Scratch/Probe.base"})
        assert not err and "parse: ok" in text and "name: Probe" in text
        err, text = await call(c, "query_base", {"path": "Scratch/Probe.base"})
        assert not err and "Computed by mcpopus" in text
        assert "1 bases" in (await call(c, "list_bases", {"folder": "Scratch"}))[1]
    assert all(w == "scratch/probe.base" or w.startswith("h:") for w in couch.writes)  # nothing else was touched


async def test_overwrite_is_refused_unless_asked_and_keeps_ctime(couch, tmp_path):
    other = GOOD.replace("Probe", "Second")
    async with await run(couch, tmp_path) as (c, _, _):
        await call(c, "write_base", {"path": "Scratch/Probe.base", "content": GOOD})
        first = dict(couch.docs["scratch/probe.base"])
        n = len(couch.writes)
        err, text = await call(c, "write_base", {"path": "Scratch/Probe.base", "content": other})
        assert err and "already exists" in text and "overwrite" in text and len(couch.writes) == n  # nothing stored at all
        err, text = await call(c, "write_base", {"path": "scratch/PROBE.base", "content": other})  # another spelling is the same file
        assert err and "already exists" in text
        err, text = await call(c, "write_base", {"path": "Scratch/Probe.base", "content": other, "overwrite": "no"})
        assert err and len(couch.writes) == n  # the host reads "no" as false; only true replaces
        err, text = await call(c, "write_base", {"path": "Scratch/Probe.base", "content": other, "overwrite": True})
        assert not err and "Base replaced" in text and f"sha256 before: {sha(GOOD.encode())}" in text and f"sha256 after: {sha(other.encode())}" in text
        doc = couch.docs["scratch/probe.base"]
        assert doc["ctime"] == first["ctime"] and doc["_rev"] != first["_rev"] and doc["_id"] == first["_id"]
        assert stored_bytes(couch, "scratch/probe.base") == other.encode()
        assert "name: Second" in (await call(c, "read_base", {"path": "Scratch/Probe.base"}))[1]


async def test_edit_is_an_exact_unique_replace_and_the_result_is_checked(couch, tmp_path):
    async with await run(couch, tmp_path) as (c, _, _):
        await call(c, "write_base", {"path": "Scratch/Probe.base", "content": GOOD})
        n = len(couch.writes)
        for old, new, fragment in [("nothing like this", "x", "not found"), ("file.", "x", "more than once"), ("", "x", "required"),
                                   ("type: table", "type: kanban", "type must be"), ("limit: 20", "limit: 0", "limit"), ("views:", "bogus:", "top-level"),
                                   ("name: Probe", "name: [", "YAML")]:
            err, text = await call(c, "edit_base", {"path": "Scratch/Probe.base", "old_text": old, "content": new})
            assert err and fragment in text, (old, text)
        assert len(couch.writes) == n  # every refusal stored nothing
        err, text = await call(c, "edit_base", {"path": "Scratch/Probe.base", "old_text": "limit: 20", "content": "limit: 5"})
        assert not err and "Base edited" in text and f"sha256 before: {sha(GOOD.encode())}" in text
        new = GOOD.replace("limit: 20", "limit: 5").encode()
        assert f"sha256 after: {sha(new)}" in text and stored_bytes(couch, "scratch/probe.base") == new
        err, text = await call(c, "edit_base", {"path": "Scratch/Missing.base", "old_text": "a", "content": "b"})
        assert err and "No such base" in text


async def test_edit_refuses_a_base_that_already_carries_unverified_keys(couch, tmp_path):
    odd = "views:\n  - type: table\n    name: Odd\n    columnSize:\n      file.name: 120\n"
    async with await run(couch, tmp_path, extra=[base_docs("Scratch/Odd.base", odd)]) as (c, _, _):
        err, text = await call(c, "edit_base", {"path": "Scratch/Odd.base", "old_text": "Odd", "content": "Even"})
        assert err and "key that is not one of" in text and "Nothing was stored" in text
    assert couch.writes == []


async def test_stale_revision_raises_a_conflict_through_the_vault(couch, tmp_path):
    async with await run(couch, tmp_path, extra=[base_docs("Scratch/Probe.base", GOOD)]) as (c, lp, _):
        v = lp.plugin.vault
        meta, _ = await v.read_base("Scratch/Probe.base")
        change = base_docs("Scratch/Probe.base", GOOD.replace("Probe", "Elsewhere"))
        change[-1]["_rev"] = "2-" + "e" * 32
        couch.load(change)
        n = len(couch.writes)
        with pytest.raises(Exception, match="changed by someone else"):
            await v.put_base("Scratch/Probe.base", GOOD.replace("Probe", "Mine").encode(), base_rev=meta.rev, must_exist=True)
        assert len(couch.writes) == n and "Elsewhere" in (await v.read_base("Scratch/Probe.base"))[1]


async def test_paths_folders_and_hidden_places(couch, tmp_path):
    async with await run(couch, tmp_path, extra=[base_docs("Medical/Records.base", GOOD)]) as (c, _, _):
        for path, fragment in [("Scratch/.obsidian/x.base", "Dot-folders"), ("Scratch/.hidden.base", "Dot-folders"), (".obsidian/x.base", "Dot-folders"), ("Scratch/x.md", "end in .base"),
                               ("Scratch/x.BASE", "end in .base"), ("Scratch/x.base.md", "end in .base"), ("Scratch/x.base/", "end in .base"), ("../x.base", "Invalid base path"),
                               ("/Scratch/x.base", "Invalid base path"), ("Scratch/a:b.base", "Invalid base path"), ("Scratch//x.base", "Invalid base path"),
                               ("Books/x.base", "not enabled for that folder"), ("x.base", "not enabled for that folder"),
                               ("Medical/x.base", "not enabled for that folder"), ("Medical/Records.base", "not enabled for that folder")]:
            for tool, args in (("write_base", {"path": path, "content": GOOD, "overwrite": True}), ("edit_base", {"path": path, "old_text": "a", "content": "b"})):
                err, text = await call(c, tool, args)
                assert err and fragment in text, (tool, path, text)
    assert couch.writes == []


async def test_a_blocked_folder_in_the_writable_list_is_still_refused(couch, tmp_path):
    async with await run(couch, tmp_path, writable_folders=["Medical", "Scratch"]) as (c, _, _):
        err, text = await call(c, "write_base", {"path": "Medical/x.base", "content": GOOD})
        assert err and "not enabled for that folder" in text
        assert not (await call(c, "write_base", {"path": "Scratch/x.base", "content": GOOD}))[0]
    assert "medical/x.base" not in couch.docs


async def test_size_text_yaml_and_structure_refusals_store_nothing(couch, tmp_path):
    big = "views:\n  - type: table\n    name: Big\n    filters: " + '"' + "x" * 200_000 + '"\n'
    cases = [(big, "larger"), ("", "empty"), ("views: [\n", "YAML"), ("a: &x 1\nb: *x\nviews:\n  - type: table\n", "YAML"), ("views:\n  - !!python/object:os.system x\n", "YAML"),
             ("- a\n- b\n", "YAML"), ("views:\n  - type: table\n---\nviews: []\n", "YAML"), ("views:\n  - type: table\n    name: \"\x01\"\n", "control"),
             ("views:\n  - type: table\n    columnSize: {}\n", "key that is not one of"), ("title: x\nviews:\n  - type: table\n", "top-level")]
    async with await run(couch, tmp_path) as (c, _, _):
        for text_in, fragment in cases:
            err, text = await call(c, "write_base", {"path": "Scratch/Bad.base", "content": text_in})
            assert err and fragment in text and "Nothing was stored" in text, (fragment, text)
            assert "x" * 50 not in text
    assert couch.writes == []


async def test_crlf_is_converted_and_reported(couch, tmp_path):
    async with await run(couch, tmp_path) as (c, _, _):
        err, text = await call(c, "write_base", {"path": "Scratch/Win.base", "content": "views:\r\n  - type: table\r\n    name: W"})
        assert not err and "converted to LF" in text
        assert stored_bytes(couch, "scratch/win.base") == b"views:\n  - type: table\n    name: W\n"


async def test_a_multi_chunk_base_with_unicode_round_trips(couch, tmp_path):
    text = ("formulas:\n" + "".join(f"  f{i}: '" + "café ☕ 日本語 🐙 " * 12 + f"{i}'\n" for i in range(200)) + "views:\n  - type: table\n    name: Long\n")
    data = text.encode()
    assert 50_000 < len(data) < 100_000
    async with await run(couch, tmp_path) as (c, lp, _):
        err, out = await call(c, "write_base", {"path": "Scratch/Long.base", "content": text})
        assert not err, out
        doc = couch.docs["scratch/long.base"]
        assert len(doc["children"]) >= 3 and doc["size"] == len(data)
        assert all(len(base64.b64decode(couch.docs[c_]["data"])) <= writer.CHUNK_BYTES for c_ in doc["children"])
        assert stored_bytes(couch, "scratch/long.base") == data
        out = (await call(c, "read_base", {"path": "Scratch/Long.base"}))[1]  # the host caps what it returns, so compare the header, not the whole text
        assert f"size: {len(data)} bytes" in out and "parse: ok" in out
        meta, back = await lp.plugin.vault.read_base("Scratch/Long.base")
        assert back == text


async def test_identical_chunks_are_not_stored_twice(couch, tmp_path):
    async with await run(couch, tmp_path) as (c, _, _):
        await call(c, "write_base", {"path": "Scratch/A.base", "content": GOOD})
        before = [w for w in couch.writes if w.startswith("h:")]
        await call(c, "write_base", {"path": "Scratch/B.base", "content": GOOD})
        assert [w for w in couch.writes if w.startswith("h:")] == before
        assert couch.docs["scratch/a.base"]["children"] == couch.docs["scratch/b.base"]["children"]


async def test_notes_and_other_documents_are_never_touched(couch, tmp_path):
    async with await run(couch, tmp_path) as (c, _, _):
        snapshot = {k: json.dumps(v, sort_keys=True) for k, v in couch.docs.items()}
        await call(c, "write_base", {"path": "Scratch/Probe.base", "content": GOOD})
        await call(c, "edit_base", {"path": "Scratch/Probe.base", "old_text": "limit: 20", "content": "limit: 9"})
        changed = {k for k, v in couch.docs.items() if snapshot.get(k) != json.dumps(v, sort_keys=True)}
        assert {k for k in changed if not k.startswith("h:")} == {"scratch/probe.base"}


async def test_note_tools_still_refuse_a_base_path(couch, tmp_path):
    async with await run(couch, tmp_path, extra=[base_docs("Scratch/Probe.base", GOOD)]) as (c, _, _):
        for tool, args in (("write_note", {"path": "Scratch/Probe.base", "content": "x"}), ("edit_note", {"path": "Scratch/Probe.base", "content": "x"}),
                           ("move_note", {"from_path": "Scratch/Probe.base", "to_path": "Scratch/P.md"}), ("delete_note", {"path": "Scratch/Probe.base"})):
            err, text = await call(c, tool, args)
            assert err and "end in .md" in text, (tool, text)
    assert couch.writes == []


async def test_rate_limit_and_counters(couch, tmp_path):
    async with await run(couch, tmp_path) as (c, _, _):
        results = [await call(c, "write_base", {"path": f"Scratch/R{i}.base", "content": GOOD}) for i in range(12)]
        assert [e for e, _ in results].count(False) == 10 and "Too many base writes" in results[10][1]
        assert "scratch/r10.base" not in couch.docs and "scratch/r9.base" in couch.docs


async def test_the_database_gate_applies_to_bases(couch, tmp_path):
    docs = build_vault("plain")
    for d in docs:
        if d["_id"] == "obsydian_livesync_version":
            d["version"] = 99
    async with await run(couch, tmp_path) as (c, _, _):
        pass
    couch.docs.clear(); couch.changes.clear(); couch.load(docs)
    async with running_plugin(tmp_path / "g", "obsidian", vals(couch), SECRETS, policy=BOTH) as (c, _, _):
        err, text = await call(c, "write_base", {"path": "Scratch/Probe.base", "content": GOOD})
        assert err and "Writing is not available" in text and "newer LiveSync generation" in text
    assert couch.writes == []


async def test_existing_bases_must_follow_the_vaults_naming(couch, tmp_path):
    odd = base_docs("Other/Weird.base", GOOD)
    odd[-1]["_id"] = "something/else"
    async with await run(couch, tmp_path, extra=[odd]) as (c, _, _):
        err, text = await call(c, "write_base", {"path": "Scratch/Probe.base", "content": GOOD})
        assert err and "named differently" in text
    assert couch.writes == []


async def test_nothing_from_the_text_is_logged_or_audited(couch, tmp_path, caplog):
    secret = "zzq-secret-filter-text"
    text = GOOD.replace('file.inFolder("Test")', f'file.name == "{secret}"')
    async with await run(couch, tmp_path) as (c, lp, _):
        err, out = await call(c, "write_base", {"path": "Scratch/S.base", "content": text})
        assert not err and secret not in out
    assert secret not in caplog.text


# ------------------------------------------------------------------ encrypted vaults
async def test_writing_a_base_to_an_encrypted_vault_with_plain_paths(couch, tmp_path):
    crypto = plugin_module("crypto")
    async with await run_e2ee(couch, tmp_path, policy=BOTH, writable_folders=["Scratch"]) as (c, lp, _):
        err, text = await call(c, "write_base", {"path": "Scratch/Enc.base", "content": GOOD})
        assert not err, text
        doc = couch.docs["scratch/enc.base"]
        assert doc["type"] == "newnote" and doc["path"] == "Scratch/Enc.base" and doc["size"] == len(GOOD.encode()) and doc["eden"] == {}
        salt = lp.plugin.vault.codec.pbkdf2_salt
        parts = []
        for cid in doc["children"]:
            ch = couch.docs[cid]
            assert cid.startswith("h:+") and ch["e_"] is True and ch["data"].startswith("%=") and ch["type"] == "leaf"
            assert "views" not in ch["data"]
            parts.append(base64.b64decode(crypto.decrypt(ch["data"], E2EE_PASS, salt)))  # independent decryption, then base64
        assert b"".join(parts) == GOOD.encode()
        assert "name: Probe" in (await call(c, "read_base", {"path": "Scratch/Enc.base"}))[1]
        assert not (await call(c, "edit_base", {"path": "Scratch/Enc.base", "old_text": "Probe", "content": "Edited"}))[0]
        assert "name: Edited" in (await call(c, "read_base", {"path": "Scratch/Enc.base"}))[1]
    raw = json.dumps(list(couch.docs.values()))
    assert "Probe" not in raw and "inFolder" not in raw


async def test_the_vault_layer_checks_the_folder_itself(couch, tmp_path):
    """Defence in depth: put_base must refuse without relying on the tool's earlier check."""
    async with await run(couch, tmp_path) as (c, lp, _):
        v = lp.plugin.vault
        for path in ("Books/x.base", "Medical/x.base", "Scratch/.obsidian/x.base", "x.base"):
            with pytest.raises(Exception, match="not enabled|Dot-folders"):
                await v.put_base(path, GOOD.encode())
    assert couch.writes == []


async def test_data_that_does_not_read_back_identically_is_never_sent(couch, tmp_path, monkeypatch):
    import sys

    real = writer.build_binary

    def corrupt(*a, **kw):
        chunks, meta = real(*a, **kw)
        chunks[0]["data"] = base64.b64encode(b"something else entirely").decode()
        return chunks, meta

    async with await run(couch, tmp_path) as (c, lp, _):
        used = sys.modules[type(lp.plugin.vault).__module__].writer  # the copy of the writer module that the vault really calls
        real = used.build_binary
        monkeypatch.setattr(used, "build_binary", corrupt)
        err, text = await call(c, "write_base", {"path": "Scratch/Probe.base", "content": GOOD})
        assert err and "internal check" in text
    assert couch.writes == []

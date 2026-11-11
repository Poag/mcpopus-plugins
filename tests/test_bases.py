"""Bases (.base) support, phase 1: real mcpopus manager, real MCP client, fake CouchDB over HTTP. Design: docs/DESIGN-bases.md."""

import base64
import json
import sys

import pytest

from conftest import plugin_module, running_plugin
from livesync_writer import MTIME, b64, build_vault, hkdf_encrypt, sha

SECRETS = {"password": "couch-pass-123"}
PASS = "writer-test-passphrase"

DOCS_EXAMPLE = """filters:
  or:
    - file.hasTag("tag")
    - and:
        - file.hasTag("book")
        - file.hasLink("Textbook")
    - not:
        - file.hasTag("book")
        - file.inFolder("Required Reading")
formulas:
  formatted_price: 'if(price, price.toFixed(2) + " dollars")'
  ppu: "(price / age).toFixed(2)"
properties:
  status:
    displayName: Status
  file.ext:
    displayName: Extension
summaries:
  customAverage: 'values.mean().round(3)'
views:
  - type: table
    name: "My table"
    limit: 10
    groupBy:
      property: note.age
      direction: DESC
    filters:
      and:
        - 'status != "done"'
        - or:
            - "formula.ppu > 5"
            - "price > 2.1"
    order:
      - file.name
      - note.age
    summaries:
      formula.ppu: Average
  - type: cards
    name: Gallery
    sort:
      - property: file.name
"""
SECRET_BASE = 'views:\n  - type: table\n    name: "secret-medical-view-name"\n    filters: \'file.name == "secret-medical-token"\'\n'


def values(couch, **kw):
    return {"couchdb_url": couch.url, "database": "vault", "username": "alex", "vault_name": "BorkBork", **kw}


async def call(client, tool, args=None):
    res = await client.call_tool(tool, args or {})
    return res.is_error, res.content[0].text


def salt_of(docs):
    d = next((d for d in docs if d["_id"] == "_local/obsidian_livesync_sync_parameters"), None)
    return base64.b64decode(d["pbkdf2salt"]) if d else None


def base_docs(path: str, text: str, *, mode: str = "plain", doc_type: str = "newnote", chunk: int = 200, passphrase: str = PASS, salt: bytes | None = None) -> list[dict]:
    """How LiveSync stores a file it does not treat as plain text: a newnote document, chunks that are the base64 of slices of the bytes."""
    raw = text.encode()
    pieces = [raw[i: i + chunk] for i in range(0, len(raw), chunk)] or [b""]
    docs, children = [], []
    for c, piece in enumerate(pieces):
        cid = ("h:" if mode == "plain" else "h:+") + sha(path + "base" + str(c))[:24]
        data = b64(piece) if doc_type == "newnote" else piece.decode()
        doc = {"_id": cid, "_rev": "1-aa", "type": "leaf", "data": data}
        if mode != "plain":
            doc["data"], doc["e_"] = hkdf_encrypt(data, passphrase, salt), True
        docs.append(doc)
        children.append(cid)
    meta = {"_id": path.lower(), "_rev": "1-" + sha(path)[:32], "type": doc_type, "path": path, "children": children,
            "ctime": MTIME - 1000, "mtime": MTIME + 7000, "size": len(raw), "eden": {}}
    if mode == "hkdf":
        props = json.dumps({"path": path, "mtime": meta["mtime"], "ctime": meta["ctime"], "size": meta["size"], "children": children})
        meta.update(_id="f:" + sha(path), path="/\\:" + hkdf_encrypt(props, passphrase, salt), mtime=0, ctime=0, size=0, children=[])
    docs.append(meta)
    return docs


def note_docs(path: str, text: str) -> list[dict]:
    cid = "h:" + sha(path + "note")[:24]
    return [{"_id": cid, "_rev": "1-aa", "type": "leaf", "data": text},
            {"_id": path.lower(), "_rev": "1-" + sha(path)[:32], "type": "plain", "path": path, "children": [cid], "ctime": MTIME, "mtime": MTIME + 3000,
             "size": len(text.encode()), "eden": {}}]


def vault_with_bases(couch):
    couch.load(build_vault("plain"))
    couch.load(base_docs("Projects/Index.base", DOCS_EXAMPLE))
    couch.load(base_docs("Projects/Archive/Old.base", "views:\n  - type: list\n    name: Old\n", doc_type="plain"))
    couch.load(base_docs("Medical/Records.base", SECRET_BASE))
    couch.load(base_docs("Top.base", "views:\n  - type: table\n    name: Top\n"))
    couch.load(note_docs("Projects/Overview.md", "# Overview\n![[Index.base#My table]] and [[Projects/Archive/Old.base|old]] and [t](Top.base)\n"
                         "Missing one: ![[Nothing.base]]\nBlocked one: ![[Records.base]]\n```\n![[Code.base]]\n```\n"))
    couch.load(note_docs("Medical/Linker.md", "# Linker\n![[Index.base]] secret-medical-token\n"))


def test_base_path_rules():
    paths = plugin_module("paths")
    for ok in ("Index.base", "Projects/Index.base", "A B/c d.base"):
        assert paths.validate_base_path(ok) == ok
    for bad in ("", "/x.base", "a/../b.base", "x.md", "x.BASE", "x.base.md", ".obsidian/x.base", "a/.hidden/x.base", "a:b.base", "a//b.base", "x.base\0"):
        with pytest.raises(paths.PathError):
            paths.validate_base_path(bad)
    assert not paths.is_note_path("Index.base") and paths.is_base_path("Index.base") and not paths.is_base_path("Index.md")


async def test_new_tools_are_read_tier_and_existing_tools_remain(couch, tmp_path):
    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, lp, _):
        names = sorted(t.name for t in (await c.list_tools()).tools)
        assert names == ["get_note_metadata", "list_bases", "list_folders", "list_notes", "list_tags", "query_base", "read_base", "read_note", "search_notes", "vault_status"]
        tiers = {m.name: m.tier for m in lp.declared if m.kind == "tool"}
        assert tiers["list_bases"] == tiers["read_base"] == tiers["query_base"] == "read"
        assert {n: t for n, t in tiers.items() if t != "read"} == {"write_note": "write", "edit_note": "write", "move_note": "write", "delete_note": "admin", "write_base": "write", "edit_base": "write"}
        assert {m.name for m in lp.declared if m.kind == "tool" and m.untrusted} >= {"list_bases", "read_base", "query_base"}


async def test_list_and_read_bases_multichunk_newnote_and_plain(couch, tmp_path):
    vault_with_bases(couch)
    async with running_plugin(tmp_path, "obsidian", values(couch, blocked_folders=["Medical"]), SECRETS) as (c, _, _):
        err, text = await call(c, "list_bases")
        assert not err and "3 bases" in text and "Projects/Index.base" in text and "Projects/Archive/Old.base" in text and "Top.base" in text
        assert "file=Projects%2FIndex.base" in text  # the extension stays in the open link
        err, text = await call(c, "read_base", {"path": "Projects/Index.base"})
        assert not err and "parse: ok" in text and "untrusted-data" in text
        assert DOCS_EXAMPLE in text  # several chunks, each base64 of a slice, reassemble exactly
        assert "views (2):" in text and "'My table'" in text and "group by note.age DESC" in text and "formulas (2):" in text
        assert "ppu: (price / age).toFixed(2)" in text and "other keys: sort" in text  # keys we do not interpret are only named
        assert "and(status != \"done\"; or(formula.ppu > 5; price > 2.1))" in text
        err, text = await call(c, "read_base", {"path": "projects/archive/old.base"})
        assert not err and "type: list" in text and "'Old'" in text  # a plain-typed document and a case-insensitive path


async def test_paging_and_filters(couch, tmp_path):
    couch.load(build_vault("plain"))
    for n in range(5):
        couch.load(base_docs(f"Many/b{n}.base", "views: []\n"))
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, _, _):
        _, text = await call(c, "list_bases", {"limit": 2})
        assert "5 bases (showing 1 to 2). Use offset 2 for the next page." in text
        _, text = await call(c, "list_bases", {"limit": 2, "offset": 4})
        assert "(showing 5 to 5)." in text and "next page" not in text
        _, text = await call(c, "list_bases", {"offset": 50})  # found on the live run: this used to say "showing 51 to 50"
        assert "5 bases (none at this offset)." in text and "next page" not in text
        _, text = await call(c, "list_bases", {"name": "no-such-base"})
        assert "0 bases (none)." in text
        _, text = await call(c, "list_bases", {"folder": "Many", "name": "b3"})
        assert "1 bases (showing 1 to 1)." in text and "Many/b3.base" in text
        assert (await call(c, "list_bases", {"folder": "../x"}))[0] is True
        assert (await call(c, "list_bases", {"name": "x" * 101}))[0] is True


async def test_blocked_bases_are_missing_everywhere(couch, tmp_path):
    vault_with_bases(couch)
    async with running_plugin(tmp_path, "obsidian", values(couch, blocked_folders=["Medical"]), SECRETS) as (c, _, _):
        outputs = [(await call(c, t, a))[1] for t, a in (
            ("list_bases", {}), ("list_bases", {"folder": "Medical"}), ("read_base", {"path": "Projects/Index.base"}),
            ("get_note_metadata", {"path": "Projects/Overview.md"}), ("vault_status", {}), ("list_notes", {}), ("list_folders", {}),
            ("search_notes", {"terms": ["medical-view-nam", "Records", "medical-tok"]}))]
        assert not any("secret-medical" in o for o in outputs)
        assert not any("Medical" in o for i, o in enumerate(outputs) if i != 4)  # the status shows the folder policy; nothing else may name the folder
        blocked = await call(c, "read_base", {"path": "Medical/Records.base"})
        missing = await call(c, "read_base", {"path": "Medical/Nothing.base"})
        elsewhere = await call(c, "read_base", {"path": "Nowhere/Nothing.base"})
        assert blocked == missing == elsewhere and blocked[0] is True and "No such base." in blocked[1]
        assert (await call(c, "list_bases", {"folder": "Medical"}))[0] is True  # a blocked folder is "No such folder." as for notes
        # a blocked note that embeds a base is not listed as linking it
        assert "Medical" not in (await call(c, "read_base", {"path": "Projects/Index.base"}))[1]


async def test_readable_folders_limit_bases(couch, tmp_path):
    vault_with_bases(couch)
    async with running_plugin(tmp_path, "obsidian", values(couch, readable_folders=["Projects"]), SECRETS) as (c, _, _):
        _, text = await call(c, "list_bases")
        assert "Projects/Index.base" in text and "Top.base" not in text and "Records" not in text
        assert (await call(c, "read_base", {"path": "Top.base"}))[0] is True
        assert (await call(c, "read_base", {"path": "Projects/Index.base"}))[0] is False


async def test_status_counts_only(couch, tmp_path):
    vault_with_bases(couch)
    couch.load([{"_id": ".obsidian/x.base", "_rev": "1-aa", "type": "newnote", "path": ".obsidian/x.base", "children": [], "ctime": 1, "mtime": 1, "size": 0, "eden": {}}])
    async with running_plugin(tmp_path, "obsidian", values(couch, blocked_folders=["Medical"]), SECRETS) as (c, _, _):
        status = json.loads((await call(c, "vault_status"))[1])
        assert status["bases"] == {"in_vault": 4, "readable_here": 3, "not_readable_here": 1, "stored_as": {"newnote": 3, "plain": 1}, "not_served": 1}
        ext = status["documents_by_extension"]
        assert ext["base"] == 5 and ext["png"] == 1 and ext["md"] >= 7
        raw = json.dumps(status)
        assert "Records" not in raw and "Index.base" not in raw and "secret-medical" not in raw
        assert status["notes_in_vault"] == ext["md"] == 7  # the existing note counts keep their meaning


async def test_extensions_beyond_the_cap_are_summed(couch, tmp_path):
    couch.load(build_vault("plain"))
    for n in range(25):
        couch.load([{"_id": f"f{n}.x{n:02d}", "_rev": "1-aa", "type": "newnote", "path": f"f{n}.x{n:02d}", "children": [], "ctime": 1, "mtime": 1, "size": 0, "eden": {}}])
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, _, _):
        ext = json.loads((await call(c, "vault_status"))[1])["documents_by_extension"]
        assert len(ext) == 21 and ext["(other extensions)"] > 0


async def test_malformed_oversized_and_refused_yaml_still_return_raw_text(couch, tmp_path, tmp_path_factory):
    import yaml

    pwn_dir = tmp_path_factory.mktemp("effects") / "pwned"
    payload = f'views: !!python/object/apply:os.mkdir ["{pwn_dir}"]\n'
    bomb = "a: &a [x,x,x,x,x,x,x,x,x]\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\nc: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\nd: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]\ne: [*d,*d,*d,*d,*d,*d,*d,*d,*d]\n"
    cases = {
        "Bad/open.base": ("views: [unclosed\n", "parse: failed (not valid YAML near line"),
        "Bad/tabs.base": ("views:\n\t- type: table\n", "parse: failed"),
        "Bad/list.base": ("- just\n- a list\n", "parse: failed (the top level of a base must be a mapping)"),
        "Bad/two.base": ("a: 1\n---\nb: 2\n", "parse: failed"),
        "Bad/alias.base": (bomb, "parse: refused (anchors and aliases are not accepted)"),
        "Bad/merge.base": ("base: &b {a: 1}\nview:\n  <<: *b\n", "parse: refused (anchors and aliases are not accepted)"),
        "Bad/tag.base": (payload, "parse: refused (the document uses a YAML tag that is not accepted)"),
        "Bad/name.base": ("views: !!python/name:os.system\n", "parse: refused"),
        "Bad/deep.base": ("a: " + "[" * 40 + "]" * 40 + "\n", "parse: refused (the document is nested too deeply)"),
        "Bad/empty.base": ("", "parse: ok"),
        "Bad/odd.base": ("views: 5\nfilters: [1, 2]\nformulas: x\n", "parse: ok"),
    }
    couch.load(build_vault("plain"))
    for path, (text, _) in cases.items():
        couch.load(base_docs(path, text))
    # a mutation check: the same payload does run under an unsafe loader, so the "must not exist" assertion below can fail
    probe = pwn_dir.parent / "probe"
    yaml.load(f'x: !!python/object/apply:os.mkdir ["{probe}"]\n', Loader=yaml.UnsafeLoader)
    assert probe.exists()
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, _, _):
        for path, (text, expect) in cases.items():
            err, out = await call(c, "read_base", {"path": path})
            assert not err, path
            assert expect in out, (path, out[:300])
            if text.strip():
                assert text in out, path  # the raw text is always returned
    assert not pwn_dir.exists()


async def test_oversized_base_is_refused_not_cut(couch, tmp_path):
    couch.load(build_vault("plain"))
    big = "views:\n" + "".join(f"  - type: table\n    name: v{n}\n" for n in range(120))
    couch.load(base_docs("Big.base", big))
    couch.load(base_docs("Small.base", "views: []\n"))
    async with running_plugin(tmp_path, "obsidian", values(couch, max_base_bytes=1000), SECRETS) as (c, _, _):
        err, text = await call(c, "read_base", {"path": "Big.base"})
        assert err and "larger than the configured limit" in text
        assert (await call(c, "read_base", {"path": "Small.base"}))[0] is False
        assert "Big.base" in (await call(c, "list_bases"))[1]  # still listed


async def test_without_a_yaml_library_the_raw_text_is_shown(couch, tmp_path, monkeypatch):
    couch.load(build_vault("plain"))
    couch.load(base_docs("Idx.base", DOCS_EXAMPLE))
    monkeypatch.setitem(sys.modules, "yaml", None)  # `import yaml` now raises ImportError, as in the host image
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, _, _):
        err, text = await call(c, "read_base", {"path": "Idx.base"})
        assert not err and "parse: unavailable (PyYAML is missing: install the plugin's packages from its page" in text and DOCS_EXAMPLE in text and "summary:" not in text
        assert (await call(c, "list_bases"))[0] is False


async def test_base_links_resolve_and_blocked_equals_missing(couch, tmp_path):
    vault_with_bases(couch)
    async with running_plugin(tmp_path, "obsidian", values(couch, blocked_folders=["Medical"]), SECRETS) as (c, _, _):
        err, text = await call(c, "get_note_metadata", {"path": "Projects/Overview.md"})
        line = next(line for line in text.splitlines() if line.startswith("bases:"))
        assert "Index.base (found)" in line and "Projects/Archive/Old.base (found)" in line and "Top.base (found)" in line
        assert "Nothing.base (not found)" in line and "Records.base (not found)" in line  # blocked answers like missing
        assert "Code.base" not in line  # inside a code fence it is not a link
        # a note with no base links gains no extra line
        assert "bases:" not in (await call(c, "get_note_metadata", {"path": "Other Note.md"}))[1]
        err, text = await call(c, "read_base", {"path": "Projects/Index.base"})
        assert "linked or embedded from (1): Projects/Overview.md" in text
        _, text = await call(c, "read_base", {"path": "Projects/Archive/Old.base"})
        assert "(1): Projects/Overview.md" in text
        _, text = await call(c, "read_base", {"path": "Top.base"})
        assert "linked or embedded from (1): Projects/Overview.md" in text


async def test_note_tools_do_not_accept_bases_or_see_their_text(couch, tmp_path):
    vault_with_bases(couch)
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, lp, _):
        for tool in ("read_note", "get_note_metadata"):
            assert (await call(c, tool, {"path": "Projects/Index.base"}))[0] is True
        _, text = await call(c, "list_notes")
        assert ".base" not in text
        _, text = await call(c, "search_notes", {"terms": ["formatted_price", "customAverage"]})
        assert "0 notes match" in text
        for tool in ("write_note", "edit_note"):  # not served by default, and refused by the plugin if they were
            assert tool not in lp.tools


async def test_nothing_is_written_and_no_secret_is_logged(couch, tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    vault_with_bases(couch)
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, _, _):
        for tool, args in (("list_bases", {}), ("read_base", {"path": "Projects/Index.base"}), ("get_note_metadata", {"path": "Projects/Overview.md"}), ("vault_status", {})):
            await call(c, tool, args)
    assert couch.log and not any(m in ("PUT", "DELETE") for m, _ in couch.log) and not couch.writes
    assert all(m == "GET" or p in ("/vault/_all_docs", "/vault/_changes") for m, p in couch.log)
    assert "couch-pass-123" not in caplog.text and "customAverage" not in caplog.text
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "Index.base" not in raw and "customAverage" not in raw


async def test_encrypted_vaults_serve_bases(couch, tmp_path):
    for mode in ("e2ee", "hkdf"):
        couch.docs.clear()
        couch.changes.clear()
        docs = build_vault(mode, PASS)
        couch.load(docs)
        couch.load(base_docs("Dir/Enc.base", DOCS_EXAMPLE, mode=mode, salt=salt_of(docs)))
        async with running_plugin(tmp_path / mode, "obsidian", values(couch), {**SECRETS, "passphrase": PASS}) as (c, _, _):
            err, text = await call(c, "read_base", {"path": "Dir/Enc.base"})
            assert not err and DOCS_EXAMPLE in text and "parse: ok" in text, mode
            assert "Dir/Enc.base" in (await call(c, "list_bases"))[1]
            assert json.loads((await call(c, "vault_status"))[1])["bases"]["in_vault"] == 1


async def test_changes_to_bases_are_picked_up(couch, tmp_path):
    couch.load(build_vault("plain"))
    couch.load(base_docs("Live.base", "views:\n  - type: table\n    name: First\n"))
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, lp, _):
        assert "'First'" in (await call(c, "read_base", {"path": "Live.base"}))[1]
        meta = couch.docs["live.base"]
        chunk = couch.docs[meta["children"][0]]
        new = "views:\n  - type: cards\n    name: Second\n"
        couch.put({**chunk, "_rev": "2-x", "data": b64(new.encode())})
        couch.put({**meta, "_rev": "2-y", "mtime": meta["mtime"] + 5000, "size": len(new)})
        lp.plugin.vault.stats.last_refresh = 0
        assert "'Second'" in (await call(c, "read_base", {"path": "Live.base"}))[1]
        couch.remove("live.base")
        lp.plugin.vault.stats.last_refresh = 0
        assert (await call(c, "read_base", {"path": "Live.base"}))[0] is True
        assert json.loads((await call(c, "vault_status"))[1])["bases"]["in_vault"] == 0


async def test_leak_check_would_catch_a_missing_policy_check(couch, tmp_path):
    """Mutation check: with the folder policy removed from the base lookups, the leakage assertions above must fail."""
    vault_with_bases(couch)
    secret = "secret-medical-view-name"
    async with running_plugin(tmp_path / "ok", "obsidian", values(couch, blocked_folders=["Medical"]), SECRETS) as (c, lp, _):
        assert secret not in (await call(c, "read_base", {"path": "Medical/Records.base"}))[1]
        assert "Records.base" not in (await call(c, "list_bases"))[1]
    async with running_plugin(tmp_path / "broken", "obsidian", values(couch, blocked_folders=["Medical"]), SECRETS) as (c, lp, _):
        v = lp.plugin.vault
        v.policy = type(v.policy)()  # the mutation: nothing is blocked any more
        assert secret in (await call(c, "read_base", {"path": "Medical/Records.base"}))[1]
        assert "Records.base" in (await call(c, "list_bases"))[1]

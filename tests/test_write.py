"""Writing to a LiveSync vault: the document shapes, safety gates, and the folder switches."""

import json

import pytest

from conftest import plugin_module, running_plugin
from livesync_writer import NOTES, build_vault
from mcpopus.pluginconfig import Policy

writer = plugin_module("writer")
xxh = plugin_module("xxh64")
SECRETS = {"password": "couch-pass-123"}
ALL = Policy(read_only=False, enable_tools=["write_note", "edit_note", "move_note", "delete_note"])
SCRATCH = ["Scratch"]


def vals(couch, **kw):
    return {"couchdb_url": couch.url, "database": "vault", "username": "alex", "vault_name": "BorkBork", "writable_folders": SCRATCH,
            "blocked_folders": ["Medical"], **kw}


async def call(c, tool, args=None):
    r = await c.call_tool(tool, args or {})
    return r.is_error, r.content[0].text


async def run(couch, tmp, policy=ALL, docs=None, **kw):
    couch.docs.clear()
    couch.changes.clear()
    couch.load(docs or build_vault("plain"))
    return running_plugin(tmp, "obsidian", vals(couch, **kw), SECRETS, policy=policy)


def test_chunk_id_matches_the_librarys_vector_and_counts_utf16_units():
    assert writer.chunk_id("helloWorld") == "h:37me21jj44pcc"  # livesync-commonlib HashManager.unit.spec.ts, xxhash64, no encryption
    assert writer.js_length("a😀") == 3 and writer.js_length("café") == 4
    assert writer.chunk_id("😀") == "h:" + writer.to_base36(xxh.xxh64("😀-2".encode()))
    assert xxh.xxh64(b"") == 0xEF46DB3751D8E999


def test_split_is_lossless_and_bounded():
    text = "# T\n" + "line of text\n" * 300 + "x" * 2500 + "\n" + "😀" * 700 + "\nend"
    pieces = writer.split(text)
    assert "".join(pieces) == text and all(len(p) <= writer.CHUNK_CHARS for p in pieces) and len(pieces) > 5
    assert writer.split("") == [] and writer.split("one") == ["one"]


def test_note_ids():
    assert writer.note_id("Claude/Code/X.md", case_sensitive=False, nfc=False) == "claude/code/x.md"
    assert writer.note_id("Claude/X.md", case_sensitive=True, nfc=False) == "Claude/X.md"
    assert writer.note_id("_Scratch/x.md", case_sensitive=False, nfc=False) == "/_scratch/x.md"
    assert writer.note_id("Café.md", case_sensitive=False, nfc=True) == "café.md"


async def test_write_tools_need_both_switches(couch, tmp_path):
    async with await run(couch, tmp_path / "a", policy=None) as (c, _, _):  # host policy default: read-only
        assert not {"write_note", "edit_note", "move_note", "delete_note"} & {t.name for t in (await c.list_tools()).tools}
    async with await run(couch, tmp_path / "b", writable_folders=[]) as (c, _, _):  # host allows, no writable folder
        assert {"write_note", "delete_note"} <= {t.name for t in (await c.list_tools()).tools}
        err, text = await call(c, "write_note", {"path": "Scratch/a.md", "content": "x"})
        assert err and "not enabled for that folder" in text
    assert couch.writes == []


async def test_new_note_has_the_exact_livesync_shape(couch, tmp_path):
    body = "---\ntags: [t]\n---\n" + "Line of text for chunking.\n" * 80 + "Ünïcode ☕ 🐙\n"
    async with await run(couch, tmp_path) as (c, _, _):
        err, text = await call(c, "write_note", {"path": "Scratch/Hello World.md", "content": body})
        assert not err and "created: Scratch/Hello World.md" in text and "file=Scratch%2FHello%20World" in text
        doc = couch.docs["scratch/hello world.md"]
        assert set(doc) == {"_id", "_rev", "path", "ctime", "mtime", "size", "type", "children", "eden"}
        assert doc["type"] == "plain" and doc["path"] == "Scratch/Hello World.md" and doc["eden"] == {} and "deleted" not in doc
        assert doc["size"] == len(body.encode()) and doc["ctime"] > 1_700_000_000_000 and doc["mtime"] >= doc["ctime"]
        chunks = [couch.docs[i] for i in doc["children"]]
        assert "".join(ch["data"] for ch in chunks) == body and len(chunks) > 1
        for ch in chunks:
            assert set(ch) >= {"_id", "type", "data"} and ch["type"] == "leaf" and ch["_id"] == writer.chunk_id(ch["data"]) and not ch.get("e_")
        err, back = await call(c, "read_note", {"path": "Scratch/Hello World.md"})
        assert body in back


async def test_overwrite_keeps_created_time_and_reuses_identical_chunks(couch, tmp_path):
    async with await run(couch, tmp_path) as (c, _, _):
        await call(c, "write_note", {"path": "Scratch/A.md", "content": "same text\n"})
        first = dict(couch.docs["scratch/a.md"])
        n_docs = len(couch.docs)
        err, text = await call(c, "write_note", {"path": "Scratch/A.md", "content": "same text\n"})
        assert "replaced" in text and len(couch.docs) == n_docs  # no new chunk
        again = couch.docs["scratch/a.md"]
        assert again["ctime"] == first["ctime"] and again["mtime"] >= first["mtime"] and again["_rev"].startswith("2-")
        await call(c, "write_note", {"path": "Scratch/B.md", "content": "same text\n"})
        assert couch.docs["scratch/b.md"]["children"] == first["children"]  # content-addressed: shared chunk


async def test_large_unicode_note_round_trips(couch, tmp_path):
    body = "# Big\n" + "".join(f"row {i} 日本語 😀\n" for i in range(600)) + "z" * 2600 + "\nend\n"
    async with await run(couch, tmp_path) as (c, lp, _):
        await call(c, "write_note", {"path": "Scratch/big.md", "content": body})
        vault = lp.plugin.vault
        meta, text = await vault.read("Scratch/big.md")
        assert text == body and len(meta.children) > 10


async def test_edit_note_operations(couch, tmp_path):
    async with await run(couch, tmp_path) as (c, _, _):
        await call(c, "write_note", {"path": "Scratch/E.md", "content": "---\ntitle: T\n---\nBody line\n"})

        async def body():
            return (await call(c, "read_note", {"path": "Scratch/E.md"}))[1].split("---\n\n", 1)[1]

        await call(c, "edit_note", {"path": "Scratch/E.md", "content": "appended"})
        assert "Body line\nappended" in await body()
        await call(c, "edit_note", {"path": "Scratch/E.md", "content": "first", "operation": "prepend"})
        assert "title: T\n---\nfirst\nBody line" in await body()
        await call(c, "edit_note", {"path": "Scratch/E.md", "content": "BODY", "operation": "replace", "old_text": "Body line"})
        assert "BODY" in await body() and "Body line" not in await body()
        for args, msg in (({"operation": "replace"}, "old_text is required"), ({"operation": "replace", "old_text": "nope"}, "not found"),
                          ({"operation": "replace", "old_text": "e"}, "more than once"), ({"operation": "sideways"}, "append, prepend or replace")):
            err, text = await call(c, "edit_note", {"path": "Scratch/E.md", "content": "x", **args})
            assert err and msg in text
        assert (await call(c, "edit_note", {"path": "Scratch/missing.md", "content": "x"}))[0]


async def test_a_change_made_elsewhere_is_never_overwritten(couch, tmp_path):
    async with await run(couch, tmp_path) as (c, lp, _):
        await call(c, "write_note", {"path": "Scratch/C.md", "content": "v1\n"})
        vault = lp.plugin.vault
        meta, _ = await vault.read("Scratch/C.md")
        other = dict(couch.docs["scratch/c.md"])  # someone else (Obsidian) saves a new revision
        couch.put({**other, "_rev": "9-other", "mtime": other["mtime"] + 5})
        with pytest.raises(Exception, match="changed by someone else"):
            await vault.put_note("Scratch/C.md", "mine\n", base_rev=meta.rev)
        # and at the CouchDB level: a stale revision is refused by the database itself
        real_get = vault.couch.get_doc

        async def stale(doc_id):
            d = await real_get(doc_id)
            return {**d, "_rev": "1-stale"} if d else d

        vault.couch.get_doc = stale
        err, text = await call(c, "write_note", {"path": "Scratch/C.md", "content": "mine\n"})
        assert err and "changed by someone else" in text
        # the note itself still holds the other writer's revision. (Its chunks are stored before the note document, as in
        # LiveSync, so a refused write can leave one unreferenced chunk behind; nothing points at it.)
        assert couch.docs["scratch/c.md"]["_rev"] == "9-other"
        assert all("mine" not in couch.docs[i]["data"] for i in couch.docs["scratch/c.md"]["children"])


async def test_delete_is_livesyncs_soft_delete_and_recreating_works(couch, tmp_path):
    async with await run(couch, tmp_path) as (c, _, _):
        await call(c, "write_note", {"path": "Scratch/D.md", "content": "bye\n"})
        before = dict(couch.docs["scratch/d.md"])
        err, text = await call(c, "delete_note", {"path": "Scratch/D.md"})
        assert not err and "Deleted" in text
        gone = couch.docs["scratch/d.md"]
        assert gone["deleted"] is True and gone["children"] == before["children"] and gone["mtime"] >= before["mtime"] and gone["_rev"].startswith("2-")
        assert (await call(c, "read_note", {"path": "Scratch/D.md"}))[0] is True
        assert "D.md" not in (await call(c, "list_notes", {"folder": "Scratch"}))[1]
        err, text = await call(c, "write_note", {"path": "Scratch/D.md", "content": "back\n"})
        assert not err and "created" in text and "deleted" not in couch.docs["scratch/d.md"] and couch.docs["scratch/d.md"]["_rev"].startswith("3-")
        assert (await call(c, "delete_note", {"path": "Scratch/none.md"}))[0] is True


async def test_move_note(couch, tmp_path):
    async with await run(couch, tmp_path, writable_folders=["Scratch", "Other2"]) as (c, _, _):
        await call(c, "write_note", {"path": "Scratch/M.md", "content": "move me\n"})
        await call(c, "write_note", {"path": "Scratch/Taken.md", "content": "x\n"})
        ctime = couch.docs["scratch/m.md"]["ctime"]
        assert (await call(c, "move_note", {"from_path": "Scratch/M.md", "to_path": "Scratch/Taken.md"}))[0] is True  # destination exists
        err, text = await call(c, "move_note", {"from_path": "Scratch/M.md", "to_path": "Other2/Moved.md"})
        assert not err and "Moved" in text
        assert couch.docs["other2/moved.md"]["ctime"] == ctime and couch.docs["scratch/m.md"]["deleted"] is True
        assert "move me" in (await call(c, "read_note", {"path": "Other2/Moved.md"}))[1]
        assert (await call(c, "move_note", {"from_path": "Other2/Moved.md", "to_path": "Elsewhere/x.md"}))[0] is True  # not writable
        assert "Elsewhere/x.md".lower() not in couch.docs


async def test_blocked_and_unlisted_folders_cannot_be_written(couch, tmp_path):
    async with await run(couch, tmp_path, writable_folders=["Scratch", "Medical"]) as (c, _, _):
        for tool, args in (("write_note", {"path": "Medical/x.md", "content": "x"}), ("edit_note", {"path": "Medical/Conditions/private.md", "content": "x"}),
                           ("delete_note", {"path": "Medical/Conditions/private.md"}), ("write_note", {"path": "Other Note.md", "content": "x"}),
                           ("write_note", {"path": "Claude/Code/demo.md", "content": "x"})):
            err, text = await call(c, tool, args)
            assert err and "not enabled for that folder" in text, (tool, args)
    assert couch.writes == []


async def test_invalid_paths_and_sizes(couch, tmp_path):
    async with await run(couch, tmp_path, max_write_bytes=1000) as (c, _, _):
        for bad in ("../x.md", "Scratch/../Other Note.md", "/Scratch/x.md", "Scratch/x.txt", "Scratch/.hidden/x.md", "Scratch/a:b.md", ""):
            assert (await call(c, "write_note", {"path": bad, "content": "x"}))[0] is True, bad
        err, text = await call(c, "write_note", {"path": "Scratch/big.md", "content": "x" * 2000})
        assert err and "write limit" in text
    assert couch.writes == []


async def test_refuses_encrypted_obfuscated_unknown_and_read_only_databases(couch, tmp_path):
    PASS = "writer-test-passphrase"
    async with await run(couch, tmp_path / "a", docs=build_vault("hkdf", PASS)) as (c, lp, _):
        pass
    for name, mutate, msg in (("hkdf", None, "obfuscated"), ("feature", {"version": 13, "used_features": ["brand-new-v9"]}, "does not know"),
                              ("newer", {"version": 14}, "newer LiveSync generation")):
        docs = build_vault("hkdf", PASS) if name == "hkdf" else build_vault("plain")
        if mutate:
            docs = [d if d["_id"] != "obsydian_livesync_version" else {**d, **mutate} for d in docs]
        secrets = {**SECRETS, **({"passphrase": PASS} if name == "hkdf" else {})}
        couch.docs.clear(); couch.changes.clear(); couch.load(docs)
        async with running_plugin(tmp_path / name, "obsidian", vals(couch), secrets, policy=ALL) as (c, _, _):
            err, text = await call(c, "write_note", {"path": "Scratch/x.md", "content": "x"})
            assert err and "Writing is not available" in text and msg in text, (name, text)
    assert couch.writes == []
    couch.docs.clear(); couch.changes.clear(); couch.load(build_vault("plain"))
    couch.read_only = True
    async with running_plugin(tmp_path / "ro", "obsidian", vals(couch), SECRETS, policy=ALL) as (c, _, _):
        err, text = await call(c, "write_note", {"path": "Scratch/x.md", "content": "x"})
        assert err and "cannot write" in text and "couch-pass-123" not in text
    assert couch.writes == [] and "scratch/x.md" not in couch.docs


async def test_note_ids_follow_the_vaults_own_scheme(couch, tmp_path):
    # a case-sensitive vault: ids equal paths
    docs = build_vault("plain")
    docs = [{**d, "_id": d["path"]} if d.get("type") in ("plain", "newnote") and d.get("path") in NOTES else d for d in docs]
    async with await run(couch, tmp_path / "cs", docs=docs) as (c, _, _):
        await call(c, "write_note", {"path": "Scratch/Mixed Case.md", "content": "x\n"})
        assert "Scratch/Mixed Case.md" in couch.docs and "scratch/mixed case.md" not in couch.docs
    # a vault where it cannot be told (only lower-case names): refuse rather than guess
    lower = {k.lower(): v for k, v in NOTES.items()}
    async with await run(couch, tmp_path / "lc", docs=build_vault("plain", notes=lower)) as (c, _, _):
        err, text = await call(c, "write_note", {"path": "Scratch/New.md", "content": "x\n"})
        assert err and "cannot tell how this vault names notes" in text
    # a leading underscore is stored as "/_..."
    async with await run(couch, tmp_path / "us", writable_folders=["_Inbox"]) as (c, _, _):
        await call(c, "write_note", {"path": "_Inbox/n.md", "content": "x\n"})
        assert "/_inbox/n.md" in couch.docs and couch.docs["/_inbox/n.md"]["path"] == "_Inbox/n.md"
        assert "x" in (await call(c, "read_note", {"path": "_Inbox/n.md"}))[1]


async def test_audit_log_and_instructions(couch, tmp_path):
    async with await run(couch, tmp_path, instructions="Daily notes live in Daily/. Use British English.") as (c, lp, _):
        assert c.instructions == "Daily notes live in Daily/. Use British English."
        await call(c, "write_note", {"path": "Scratch/Audit.md", "content": "PRIVATE-CONTENT-XYZ"})
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "PRIVATE-CONTENT-XYZ" not in raw and "Audit.md" not in raw
    row = next(json.loads(line) for line in raw.splitlines() if json.loads(line)["tool"] == "write_note")
    assert row["arg_names"] == ["content", "path"] and row["status"] == "ok"


E2EE_PASS = "writer-test-passphrase"
E2EE_SECRETS = {**SECRETS, "passphrase": E2EE_PASS}


async def run_e2ee(couch, tmp, docs=None, secrets=None, policy=ALL, **kw):
    couch.docs.clear()
    couch.changes.clear()
    couch.load(docs or build_vault("e2ee", E2EE_PASS))
    return running_plugin(tmp, "obsidian", vals(couch, **kw), secrets or E2EE_SECRETS, policy=policy)


async def test_status_samples_chunks_and_reports_the_real_storage_mode(couch, tmp_path):
    async with await run(couch, tmp_path / "plain") as (c, _, _):
        s = json.loads((await call(c, "vault_status"))[1])
        assert s["entry_ids"] == {"plain": 5, "obfuscated": 0} and s["chunk_encryption"] == {"none": s["sampled_chunks"]} and s["sampled_chunks"] > 0
        assert s["writes"]["mode"] == "plain" and s["writes"]["database_gate"] == "ok"
    async with await run_e2ee(couch, tmp_path / "e2ee") as (c, _, _):
        s = json.loads((await call(c, "vault_status"))[1])
        assert s["entry_ids"]["plain"] == 5 and s["chunk_encryption"] == {"hkdf": s["sampled_chunks"]} and s["sampled_chunks"] > 0  # not "none": this vault IS encrypted
        assert s["writes"]["mode"] == "hkdf" and s["writes"]["database_gate"] == "ok" and s["passphrase_configured"] is True


async def test_writing_to_an_encrypted_vault_with_plain_paths(couch, tmp_path):
    from livesync_writer import sha

    body = "# Secret plan\n" + "Line of text for chunking.\n" * 60 + "Ünïcode ☕ 🐙\n"
    crypto = plugin_module("crypto")
    async with await run_e2ee(couch, tmp_path) as (c, lp, _):
        err, text = await call(c, "write_note", {"path": "Scratch/Enc.md", "content": body})
        assert not err and "created" in text
        doc = couch.docs["scratch/enc.md"]
        assert set(doc) == {"_id", "_rev", "path", "ctime", "mtime", "size", "type", "children", "eden"} and doc["path"] == "Scratch/Enc.md"  # paths stay plain, as in the vault
        salt = lp.plugin.vault.codec.pbkdf2_salt
        plain = []
        for cid in doc["children"]:
            ch = couch.docs[cid]
            assert ch["_id"].startswith("h:+") and ch["e_"] is True and ch["type"] == "leaf" and ch["data"].startswith("%=")
            assert "Line of text" not in ch["data"] and cid != writer.chunk_id(crypto.decrypt(ch["data"], E2EE_PASS, salt))
            plain.append(crypto.decrypt(ch["data"], E2EE_PASS, salt))  # an independent decryption with the vault's passphrase and salt
        assert "".join(plain) == body
        assert body in (await call(c, "read_note", {"path": "Scratch/Enc.md"}))[1]
        # ids are keyed: the same text gets the same id, which is the same in another note (shared chunk)
        await call(c, "write_note", {"path": "Scratch/Enc2.md", "content": body})
        assert couch.docs["scratch/enc2.md"]["children"] == doc["children"]
        # edit and delete use the same path
        assert not (await call(c, "edit_note", {"path": "Scratch/Enc.md", "content": "more"}))[0]
        assert "more" in (await call(c, "read_note", {"path": "Scratch/Enc.md"}))[1]
        assert not (await call(c, "delete_note", {"path": "Scratch/Enc2.md"}))[0]
        assert couch.docs["scratch/enc2.md"]["deleted"] is True
    raw = json.dumps(list(couch.docs.values()))
    assert "Secret plan" not in raw and "Ünïcode" not in raw  # nothing readable was stored


async def test_unsupported_encryption_layouts_are_refused_with_the_reason(couch, tmp_path):
    from livesync_writer import v2_encrypt

    docs = build_vault("e2ee", E2EE_PASS)
    legacy = [{**d, "data": v2_encrypt("old chunk", E2EE_PASS)} if d.get("type") == "leaf" else d for d in docs]
    async with await run_e2ee(couch, tmp_path / "legacy", docs=legacy) as (c, _, _):
        err, text = await call(c, "write_note", {"path": "Scratch/x.md", "content": "x"})
        assert err and "layout writing does not support" in text and "v2" in text and "reading still works" in text
    assert couch.writes == []

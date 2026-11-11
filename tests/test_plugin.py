"""The plugin end to end: real mcpopus manager, real MCP client, fake CouchDB over HTTP."""

import json
import logging

import pytest

from conftest import running_plugin
from livesync_writer import NOTES, build_vault

SECRET = "secret-medical-token"
PASS = "writer-test-passphrase"


def values(couch, **kw):
    return {"couchdb_url": couch.url, "database": "vault", "username": "alex", "vault_name": "BorkBork", **kw}


SECRETS = {"password": "couch-pass-123"}


async def call(client, tool, args=None):
    res = await client.call_tool(tool, args or {})
    return res.is_error, res.content[0].text


async def test_tools_are_read_only_and_listed(couch, tmp_path):
    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, lp, _):
        names = sorted(t.name for t in (await c.list_tools()).tools)
        assert names == ["get_note_metadata", "list_bases", "list_folders", "list_notes", "list_tags", "query_base", "read_base", "read_note", "search_notes", "vault_status"]  # list_bases and read_base: 0.4.0; query_base: 0.5.0
        assert all(m.tier == "read" for m in lp.declared if m.kind == "tool" and m.name in lp.tools)  # nothing else is served
        assert {m.name: m.tier for m in lp.declared if m.tier != "read"} == {"write_note": "write", "edit_note": "write", "move_note": "write", "delete_note": "admin", "write_base": "write", "edit_base": "write"}


async def test_read_search_list_plain_vault(couch, tmp_path):
    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, _, _):
        err, text = await call(c, "read_note", {"path": "Claude/Code/demo.md"})
        assert not err and "untrusted-data" in text and "# Demo" in text and "obsidian://open?vault=BorkBork&amp;" not in text
        assert "file=Claude%2FCode%2Fdemo" in text
        err, text = await call(c, "search_notes", {"terms": ["needle"]})
        assert "2 notes match" in text and "Other Note.md" in text and "Daily/2026-11-04.md" in text
        err, text = await call(c, "list_notes", {"folder": "Claude"})
        assert "Claude/Code/demo.md" in text and "Other Note.md" not in text
        err, text = await call(c, "list_notes", {"tag": "daily"})
        assert "Daily/2026-11-04.md" in text and "demo.md" not in text
        err, text = await call(c, "list_tags")
        assert "- mcp (1)" in text and "- inline-tag (1)" in text and "not-a-tag" not in text
        err, text = await call(c, "list_folders")
        assert "- Claude/Code (1 notes)" in text and "- Claude (0 notes)" in text
        err, text = await call(c, "get_note_metadata", {"path": "Claude/Tech/server.md"})
        assert "backlinks: Claude/Code/demo.md" in text and "links: demo" in text


async def test_encrypted_obfuscated_vaults_read_through_the_plugin(couch, tmp_path):
    for mode in ("hkdf", "v1"):
        couch.docs.clear()
        couch.changes.clear()
        couch.load(build_vault(mode, PASS))
        async with running_plugin(tmp_path / mode, "obsidian", values(couch), {**SECRETS, "passphrase": PASS}) as (c, _, _):
            err, text = await call(c, "read_note", {"path": "Other Note.md"})
            assert not err and "Plain root note" in text
            err, text = await call(c, "search_notes", {"terms": ["needle"]})
            assert "2 notes match" in text
            err, text = await call(c, "vault_status")
            status = json.loads(text)
            assert status["entry_ids"]["obfuscated"] == 5 and status["notes_readable_here"] == 5


async def test_blocked_folder_is_invisible_everywhere(couch, tmp_path):
    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path, "obsidian", values(couch, blocked_folders=["Medical"]), SECRETS) as (c, lp, _):
        outputs = []
        for tool, args in (("read_note", {"path": "Claude/Code/demo.md"}), ("search_notes", {"terms": ["Private", "Sensitive", "must never leave"]}),
                           ("list_notes", {}), ("list_notes", {"folder": "Medical"}), ("list_folders", {}), ("list_tags", {}),
                           ("get_note_metadata", {"path": "Claude/Code/demo.md"}), ("vault_status", {})):
            outputs.append((await call(c, tool, args))[1])
        assert not any(SECRET in o or "Medical" in o.replace('"blocked": [\n   "medical"', "") for o in outputs[:-1])
        # reading it directly gives the same answer as a note that does not exist
        blocked = await call(c, "read_note", {"path": "Medical/Conditions/private.md"})
        missing = await call(c, "read_note", {"path": "Medical/Conditions/nothing.md"})
        elsewhere = await call(c, "read_note", {"path": "Claude/nothing.md"})
        assert blocked == missing == elsewhere and blocked[0] is True
        assert (await call(c, "list_notes", {"folder": "Medical"}))[0] is True
        # a backlink from a blocked note is not disclosed
        assert "private" not in (await call(c, "get_note_metadata", {"path": "Claude/Code/demo.md"}))[1].lower()


async def test_readable_folders_limit_the_vault(couch, tmp_path):
    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path, "obsidian", values(couch, readable_folders=["Claude/Code"]), SECRETS) as (c, _, _):
        assert (await call(c, "read_note", {"path": "Claude/Code/demo.md"}))[0] is False
        assert (await call(c, "read_note", {"path": "Other Note.md"}))[0] is True
        assert (await call(c, "read_note", {"path": "Claude/Tech/server.md"}))[0] is True
        _, text = await call(c, "list_notes")
        assert "1 notes" in text and "Other Note" not in text


async def test_changes_are_picked_up_and_deletions_removed(couch, tmp_path):
    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, lp, _):
        vault = lp.plugin.vault
        assert "1 notes" in (await call(c, "list_notes", {"name": "server"}))[1]
        meta = dict(couch.docs["claude/tech/server.md"])
        chunk = couch.docs[meta["children"][0]]
        couch.put({**chunk, "_rev": "2-x", "data": "Edited server note with #newtag\n"})
        meta["children"] = [chunk["_id"]]
        couch.put({**meta, "_rev": "2-y", "mtime": meta["mtime"] + 5000})
        vault.stats.last_refresh = 0  # skip the one-second refresh throttle
        assert "Edited server note" in (await call(c, "read_note", {"path": "Claude/Tech/server.md"}))[1]
        couch.remove("claude/tech/server.md")
        vault.stats.last_refresh = 0
        assert (await call(c, "read_note", {"path": "Claude/Tech/server.md"}))[0] is True


async def test_input_validation(couch, tmp_path):
    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, _, _):
        for bad in ("../../etc/passwd", "/etc/passwd", "a.txt", ".obsidian/app.json", "i:.obsidian/app.json", "Claude/../Medical/x.md", ""):
            err, text = await call(c, "read_note", {"path": bad})
            assert err, bad
        assert (await call(c, "search_notes", {"terms": []}))[0] is True
        assert (await call(c, "list_notes", {"folder": "../x"}))[0] is True
        assert (await call(c, "list_notes", {"modified_after": "yesterday"}))[0] is True


async def test_never_writes_to_couchdb(couch, tmp_path):
    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, _, _):
        for tool, args in (("read_note", {"path": "Other Note.md"}), ("search_notes", {"terms": ["x"]}), ("list_tags", {}), ("get_note_metadata", {"path": "Other Note.md"}), ("vault_status", {})):
            await call(c, tool, args)
    allowed_post = ("/vault/_all_docs", "/vault/_changes")
    assert couch.log and all(m == "GET" or (m == "POST" and p in allowed_post) for m, p in couch.log), couch.log
    assert not any(m in ("PUT", "DELETE") for m, _ in couch.log)


async def test_failures_are_reported_without_secrets(couch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path / "a", "obsidian", values(couch), {"password": "wrong-couch-pass"}) as (c, _, _):
        err, text = await call(c, "read_note", {"path": "Other Note.md"})
        assert err and "refused the credentials" in text and "wrong-couch-pass" not in text
    async with running_plugin(tmp_path / "b", "obsidian", {**values(couch), "database": "nope"}, SECRETS) as (c, _, _):
        err, text = await call(c, "list_notes")
        assert err and "check the database name" in text
    couch.docs.clear(); couch.changes.clear()
    couch.load(build_vault("hkdf", PASS))
    async with running_plugin(tmp_path / "c", "obsidian", values(couch), {**SECRETS, "passphrase": "totally wrong"}) as (c, _, _):
        _, text = await call(c, "list_notes")
        assert text.startswith("0 notes") and "totally wrong" not in text
        status = json.loads((await call(c, "vault_status"))[1])
        assert status["unreadable_entries"] >= 5 and status["passphrase_configured"] is True
    assert "wrong-couch-pass" not in caplog.text and "totally wrong" not in caplog.text and "couch-pass-123" not in caplog.text


async def test_audit_log_has_no_content(couch, tmp_path):
    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, _, _):
        await call(c, "read_note", {"path": "Claude/Code/demo.md"})
        await call(c, "search_notes", {"terms": ["Sensitive-term-xyz"]})
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "Sensitive-term-xyz" not in raw and "Demo" not in raw and "demo.md" not in raw
    rows = [json.loads(line) for line in raw.splitlines()]
    assert {r["tool"] for r in rows} >= {"read_note", "search_notes"} and any(r["arg_names"] == ["path"] for r in rows)


async def test_secret_fields_come_from_the_form_schema(couch, tmp_path):
    """The admin form must treat password and passphrase as secrets and offer every setting."""
    import sys

    from mcpopus.forms import describe

    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path, "obsidian", values(couch), SECRETS) as (c, lp, mgr):
        model = mgr.config_model("obsidian")
        kinds = {f.name: f.kind for f in describe(model, {}, set())}
        assert kinds["password"] == "secret" and kinds["passphrase"] == "secret" and kinds["blocked_folders"] == "lines"
        assert kinds["couchdb_url"] == "text" and kinds["max_note_bytes"] == "number"


async def test_pasted_url_with_stray_whitespace_works_and_bad_urls_are_explained(couch, tmp_path):
    from conftest import running_plugin as rp
    from mcpopus.manager import PluginLoadError

    couch.load(build_vault("plain"))
    async with rp(tmp_path / "a", "obsidian", values(couch, couchdb_url="  " + couch.url + " \n", database=" vault "), SECRETS) as (c, _, _):
        assert (await call(c, "read_note", {"path": "Other Note.md"}))[0] is False
    for bad in ("couchdb:5984", "192.168.1.5:5984", "ftp://x/y"):
        with pytest.raises(PluginLoadError, match="http"):
            async with rp(tmp_path / "b", "obsidian", values(couch, couchdb_url=bad), SECRETS):
                pass


async def test_unusable_url_and_refused_connection_have_clear_messages(couch, tmp_path):
    import httpx2

    couch.load(build_vault("plain"))
    async with running_plugin(tmp_path / "a", "obsidian", {**values(couch), "couchdb_url": "http://127.0.0.1:9"}, SECRETS) as (c, _, _):
        err, text = await call(c, "list_notes")
        assert err and "cannot connect to CouchDB" in text
    async with running_plugin(tmp_path / "b", "obsidian", values(couch), SECRETS) as (c, lp, _):
        async def boom(*a, **k):
            raise httpx2.UnsupportedProtocol("x")

        lp.plugin.vault.couch._http.request = boom
        lp.plugin.vault.stats.last_refresh = 0  # skip the refresh throttle so a request is made
        err, text = await call(c, "list_notes")
        assert err and "must start with http:// or https://" in text

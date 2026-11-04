"""Fixtures written by the real LiveSync JavaScript code (tools/gen_fixtures.mjs, run in CI).

Locally these are skipped when the fixtures have not been generated. In CI REQUIRE_FIXTURES=1 makes a missing
fixture a failure, so a silent skip cannot hide a broken interop check."""

import base64
import json
import os

import pytest

from conftest import ROOT, plugin_module, running_plugin

ls = plugin_module("livesync")
crypto = plugin_module("crypto")
paths = plugin_module("paths")
FIX = ROOT / "tests" / "fixtures"


def load(name):
    f = FIX / name
    if not f.exists():
        if os.environ.get("REQUIRE_FIXTURES") == "1":
            pytest.fail(f"{name} was not generated")
        pytest.skip("fixtures not generated (run tools/gen_fixtures.mjs)")
    return json.loads(f.read_text())


def test_every_format_decrypts_js_output():
    v = load("vectors.json")
    salt = base64.b64decode(v["pbkdf2salt"])
    for key in ("hkdf", "hkdf_salted", "v3", "v2", "v2_dynamic", "v1"):
        assert crypto.decrypt(v[key], v["passphrase"], salt) == v["plaintext"], key
    assert crypto.decrypt(v["obfuscated_path"], v["passphrase"]) == v["path"]


@pytest.mark.parametrize("mode", ["plain", "hkdf", "v1"])
def test_js_generated_vault_decodes(mode):
    data = load(f"vault_{mode}.json")
    docs = data["docs"]
    salt = next((base64.b64decode(d["pbkdf2salt"]) for d in docs if d.get("type") == "sync-parameters"), None)
    codec = ls.Codec(data["passphrase"], salt)
    chunks = {d["_id"]: d for d in docs if d.get("type") == "leaf"}
    out = {}
    for d in docs:
        meta = codec.decode_meta(d)
        if meta and paths.is_note_path(meta.path):
            out[meta.path] = codec.assemble(meta, chunks)
    assert out == data["notes"]


@pytest.mark.parametrize("mode", ["plain", "hkdf", "v1"])
async def test_plugin_reads_js_generated_vault(couch, tmp_path, mode):
    data = load(f"vault_{mode}.json")
    couch.load(data["docs"])
    secrets = {"password": "couch-pass-123", **({"passphrase": data["passphrase"]} if data["passphrase"] else {})}
    async with running_plugin(tmp_path, "obsidian", {"couchdb_url": couch.url, "database": "vault", "username": "alex", "blocked_folders": ["Medical"]}, secrets) as (c, _, _):
        res = await c.call_tool("read_note", {"path": "Claude/Code/demo.md"})
        assert not res.is_error and "End of demo." in res.content[0].text
        res = await c.call_tool("search_notes", {"terms": ["Sensitive"]})
        assert "Medical" not in res.content[0].text and "0 notes match" in res.content[0].text


def test_chunk_ids_match_the_real_xxhash():
    writer = plugin_module("writer")
    cases = load("chunk_ids.json")
    assert len(cases) >= 8
    for c in cases:
        assert writer.chunk_id(c["piece"]) == c["id"], c["piece"]


@pytest.mark.parametrize("mode", ["plain"])
async def test_notes_written_by_the_plugin_use_the_ids_livesync_would(couch, tmp_path, mode):
    """Write through the plugin into a vault made by the real JavaScript code, then read it back."""
    from mcpopus.pluginconfig import Policy

    data = load(f"vault_{mode}.json")
    docs = data["docs"] + [{"_id": "obsydian_livesync_version", "_rev": "1-cc", "type": "versioninfo", "version": 12}]
    seen, uniq = set(), []
    for d in docs:
        if d["_id"] not in seen:
            seen.add(d["_id"])
            uniq.append(d)
    couch.load(uniq)
    values = {"couchdb_url": couch.url, "database": "vault", "username": "alex", "writable_folders": ["Scratch"]}
    policy = Policy(read_only=False, enable_tools=["write_note"])
    async with running_plugin(tmp_path, "obsidian", values, {"password": "couch-pass-123"}, policy=policy) as (c, _, _):
        res = await c.call_tool("write_note", {"path": "Scratch/Interop.md", "content": "# Interop\nwritten by the plugin\n"})
        assert not res.is_error, res.content[0].text
    doc = couch.docs["scratch/interop.md"]
    writer = plugin_module("writer")
    assert doc["type"] == "plain" and doc["children"]
    assert all(i == writer.chunk_id(couch.docs[i]["data"]) and not i.startswith("h:+") for i in doc["children"])


async def test_write_fixture_for_the_javascript_check(couch, tmp_path):
    """CI only (WRITE_FIXTURES=1): have the plugin write into an encrypted vault and dump the encrypted chunks, so that
    tools/verify_written.mjs can decrypt them with the real LiveSync code (octagonal-wheels)."""
    if os.environ.get("WRITE_FIXTURES") != "1":
        pytest.skip("set WRITE_FIXTURES=1 (CI does) to produce the fixture")
    import base64

    from livesync_writer import build_vault
    from mcpopus.pluginconfig import Policy

    writer = plugin_module("writer")
    passphrase = "writer-test-passphrase"
    docs = build_vault("e2ee", passphrase)
    couch.load(docs)
    salt_b64 = next(d["pbkdf2salt"] for d in docs if d.get("type") == "sync-parameters")
    body = "# Written by the plugin\n" + "A line that will be split into several chunks.\n" * 50 + "Unicode: café ☕ 日本語 🐙\n"
    values = {"couchdb_url": couch.url, "database": "vault", "username": "alex", "writable_folders": ["Scratch"]}
    async with running_plugin(tmp_path, "obsidian", values, {"password": "couch-pass-123", "passphrase": passphrase},
                              policy=Policy(read_only=False, enable_tools=["write_note"])) as (c, _, _):
        res = await c.call_tool("write_note", {"path": "Scratch/FromPlugin.md", "content": body})
        assert not res.is_error, res.content[0].text
    meta = couch.docs["scratch/fromplugin.md"]
    pieces = writer.split(body)
    assert len(pieces) == len(meta["children"]) > 1
    (FIX).mkdir(parents=True, exist_ok=True)
    (FIX / "written_by_plugin.json").write_text(json.dumps({
        "passphrase": passphrase, "pbkdf2salt": salt_b64,
        "chunks": [{"id": cid, "data": couch.docs[cid]["data"], "e_": couch.docs[cid].get("e_"), "expected": piece} for cid, piece in zip(meta["children"], pieces)],
    }))

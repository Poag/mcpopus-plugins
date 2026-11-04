"""Codec: every storage mode the plugin claims to read."""

import base64

import pytest

from conftest import plugin_module
from livesync_writer import NOTES, b64, build_vault, compress

ls = plugin_module("livesync")
paths = plugin_module("paths")
crypto = plugin_module("crypto")
PASS = "writer-test-passphrase"


def read_all(mode: str, passphrase=PASS):
    docs = build_vault(mode, PASS)
    salt = next((base64.b64decode(d["pbkdf2salt"]) for d in docs if d.get("type") == "sync-parameters"), None)
    codec = ls.Codec(passphrase if mode != "plain" else None, salt)
    chunks = {d["_id"]: d for d in docs if d.get("type") == "leaf"}
    out = {}
    for d in docs:
        meta = codec.decode_meta(d)
        if meta and paths.is_note_path(meta.path):  # the vault applies the .md and hidden-path rules
            out[meta.path] = codec.assemble(meta, chunks)
    return out


@pytest.mark.parametrize("mode", ["plain", "hkdf", "v1"])
def test_every_mode_reads_every_note(mode):
    assert read_all(mode) == NOTES  # also proves deleted, internal, binary and control documents are skipped


def test_obfuscated_metadata_hides_path_until_decrypted():
    docs = build_vault("hkdf", PASS)
    metas = [d for d in docs if d.get("type") == "plain"]
    assert all(d["_id"].startswith("f:") and d["path"].startswith("/\\:") and d["mtime"] == 0 for d in metas if "gone" not in d["_id"] and not d["_id"].startswith("i:"))
    meta = ls.Codec(PASS, base64.b64decode(next(d["pbkdf2salt"] for d in docs if d.get("type") == "sync-parameters"))).decode_meta(metas[0])
    assert meta.obfuscated and meta.mtime > 0 and meta.children


def test_wrong_passphrase_never_returns_text():
    for mode in ("hkdf", "v1"):
        with pytest.raises(ls.LiveSyncError):
            read_all(mode, passphrase="wrong passphrase")


def test_missing_passphrase_is_reported():
    docs = build_vault("hkdf", PASS)
    with pytest.raises(ls.LiveSyncError, match="no passphrase"):
        ls.Codec(None, None).decode_meta(next(d for d in docs if d.get("type") == "plain" and d["_id"].startswith("f:")))


def test_hkdf_value_without_salt_is_reported():
    docs = build_vault("hkdf", PASS)
    meta_doc = next(d for d in docs if d.get("type") == "plain" and d["_id"].startswith("f:"))
    with pytest.raises(ls.LiveSyncError, match="PBKDF2 salt"):
        ls.Codec(PASS, None).decode_meta(meta_doc)


def test_legacy_notes_type_and_newnote_binary():
    codec = ls.Codec(None, None)
    m = codec.decode_meta({"_id": "old.md", "type": "notes", "path": "Old.md", "data": ["a", "b"], "ctime": 1, "mtime": 2, "size": 2})
    assert codec.assemble(m, {}) == "ab"
    binary = {"_id": "b.md", "type": "newnote", "path": "B.md", "children": ["h:1"], "eden": {}}
    meta = codec.decode_meta(binary)
    assert codec.assemble(meta, {"h:1": {"data": b64(b"hi \xe2\x98\x95")}}) == "hi ☕"
    with pytest.raises(ls.LiveSyncError, match="binary"):
        codec.assemble(meta, {"h:1": {"data": b64(b"\xff\xfe\x00")}})


def test_compression_roundtrip_and_damage():
    codec = ls.Codec(None, None)
    assert codec.decode_chunk({"data": compress("héllo " * 50)}) == "héllo " * 50
    assert codec.decode_chunk({"data": "\x0eLZ\x1d"}) == ""
    with pytest.raises(ls.LiveSyncError):
        codec.decode_chunk({"data": "\x0eLZ\x1dnot-base64-deflate!!"})


def test_missing_chunk_is_an_error_not_partial_text():
    docs = build_vault("plain")
    codec = ls.Codec(None, None)
    meta = next(codec.decode_meta(d) for d in docs if d.get("path") == "Claude/Code/demo.md")
    with pytest.raises(ls.LiveSyncError, match="missing"):
        codec.assemble(meta, {})


def test_hidden_and_control_paths_are_not_notes():
    codec = ls.Codec(None, None)
    for doc in ({"_id": "i:x", "type": "plain", "path": "i:.obsidian/x"}, {"_id": "ps:x", "type": "plain", "path": "ps:x"},
                {"_id": "a.md", "type": "plain", "path": "a.md", "deleted": True}, {"_id": "a.md", "type": "plain", "path": "a.md", "_deleted": True},
                {"_id": "h:1", "type": "leaf", "data": "x"}, {"_id": "_design/x", "type": "plain", "path": "x.md"}):
        assert codec.decode_meta(doc) is None


def test_classify_reports_counts_only():
    report = ls.Codec(PASS, None).classify(build_vault("hkdf", PASS))
    assert report["entry_ids"]["obfuscated"] == 5 and "hkdf" in report["chunk_encryption"]
    assert "Sensitive" not in str(report)

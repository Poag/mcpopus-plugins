"""Real vectors taken from octagonal-wheels' own test suite (src/encryption/encryption.test.ts), MIT."""

import pytest

from conftest import plugin_module

crypto = plugin_module("crypto")


def test_v2_vector():
    enc = "%ae62aa26d34338a54d41d40601000000e05454c7b864c044ccc5b8c769d8e950L6f25sLek+g+C+1eKHgLrlffdrr0ABFC6B6g7uI="
    assert crypto.decrypt(enc, "passwordTest") == "encryptedData"
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(enc, "wrong")


def test_v2_obfuscated_path_vector():
    enc = "%931636970d4bc03e843d02f0d7f6cc4cdcdcf2a062af7b8c259e3af73edb64c8v7zdTqKnYzWgW6+ry1dHkPT8aMQJtIg2WwNu3aKXalrIow=="
    assert crypto.looks_like_obfuscated_path(enc)
    assert crypto.decrypt(enc, "passwordTest") == "/path/to/obfuscate"


def test_v3_vector():
    assert crypto.decrypt("%~ddd35704b824765901296a21xi/lryj6A5QY+c/NkPSROop1p/POZ5EBQYRLkoGSHQ==", "password") == "fancy-syncying!"


def test_unknown_and_damaged():
    for bad in ("plain text", "%", "%=", "%$AAAA", "[", '["a","b"]', "%~zz"):
        with pytest.raises(crypto.DecryptError):
            crypto.decrypt(bad, "p", b"x" * 32)
    assert crypto.encryption_kind("%=abc") == "hkdf" and crypto.encryption_kind("%$a") == "hkdf-salted" and crypto.encryption_kind("hello") == "none"

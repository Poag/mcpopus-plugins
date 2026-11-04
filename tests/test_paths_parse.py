import pytest

from conftest import plugin_module

paths = plugin_module("paths")
parse = plugin_module("parse")


@pytest.mark.parametrize("p", ["a.md", "Folder/a b.md", "Claude/Code/x.md", "日本語/ノート.md"])
def test_valid_note_paths(p):
    assert paths.validate_note_path(p) == p


@pytest.mark.parametrize("p", ["", "/abs.md", "../x.md", "a/../b.md", "a.txt", "a:b.md", ".obsidian/x.md", "a/.hidden/x.md", "a//b.md", "a\0.md", "x" * 1001 + ".md", "RedFlag.md", "a\\..\\b.md"])
def test_invalid_note_paths(p):
    with pytest.raises(paths.PathError):
        paths.validate_note_path(p)


def test_folder_policy():
    pol = paths.FolderPolicy.build(["Claude", "Books/Public"], ["claude/private", "Medical"])
    assert pol.allows_path("Claude/Code/x.md") and pol.allows_path("Books/Public/a.md")
    assert not pol.allows_path("Claude/Private/x.md") and not pol.allows_path("CLAUDE/PRIVATE/x.md")  # case-insensitive, fail closed
    assert not pol.allows_path("Medical/x.md") and not pol.allows_path("Other/x.md")
    assert not pol.allows_path("Claudex/x.md")  # whole segments only
    assert pol.folder_visible("Claude/Code") and pol.folder_visible("Books") and not pol.folder_visible("Medical/Conditions") and not pol.folder_visible("Other")
    open_pol = paths.FolderPolicy.build([], ["Medical"])
    assert open_pol.allows_path("Anything/x.md") and not open_pol.allows_path("medical/x.md") and not open_pol.allows_path("Medical/a/b.md")


def test_unicode_normalisation_is_fail_closed():
    pol = paths.FolderPolicy.build([], ["Café"])  # composed
    assert not pol.allows_path("Café/x.md")  # decomposed spelling of the same folder


def test_parse_frontmatter_tags_links():
    text = "---\ntitle: T\ntags: [a, b/c]\nlist:\n---\nBody #inline and `#code` and #123.\n```\n#fenced\n```\n[[Note One|alias]] [x](dir/Two.md) [[Note One]]\n"
    p = parse.parse(text)
    assert p.frontmatter["title"] == "T" and set(p.tags) == {"a", "b/c", "inline"}
    assert p.links == ["Note One", "dir/Two.md"]
    assert parse.parse("---\ntags:\n  - x\n  - y z\n---\n").tags == ["x", "y z"]


def test_link_target_matching():
    assert parse.link_target_matches("demo", "Claude/Code/demo.md") and parse.link_target_matches("Claude/Code/demo.md", "Claude/Code/demo.md")
    assert parse.link_target_matches("Demo#heading", "Claude/Code/demo.md") and not parse.link_target_matches("emo", "Claude/Code/demo.md")

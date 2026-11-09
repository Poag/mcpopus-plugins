"""Path rules: what counts as a note path, and which folders the connector may touch."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .livesync import nfc

RESERVED_ROOT = {"redflag.md", "redflag2.md", "redflag3.md", "flag_rebuild.md", "flag_fetch.md"}


class PathError(ValueError):
    """Message is safe to show to the model."""


def validate_note_path(path: str) -> str:
    """Vault-relative, ends in .md, no traversal, no hidden segments, no LiveSync control files.
    Same rules as es617/obsidian-sync-mcp's note-path.ts (MIT), so existing habits carry over."""
    if not path or len(path) > 1000 or path.startswith("/") or "\0" in path or ".." in path:
        raise PathError("Invalid note path.")
    if ":" in path:
        raise PathError("Invalid note path: ':' is not allowed.")
    if not path.endswith(".md"):
        raise PathError("Note paths are vault-relative and end in .md.")
    segments = re.split(r"[/\\]", path)
    for seg in segments:
        if seg == "":
            raise PathError("Invalid note path: empty segment.")
        if seg.startswith("."):
            raise PathError("Dot-folders and hidden files are not available.")
    if len(segments) == 1 and path.lower() in RESERVED_ROOT:
        raise PathError("Reserved LiveSync control file.")
    return path


def is_note_path(path: str) -> bool:
    try:
        validate_note_path(path)
        return True
    except PathError:
        return False


def validate_base_path(path: str) -> str:
    """Vault-relative, ends in exactly .base, same traversal and hidden-folder rules as note paths (so never inside .obsidian)."""
    if not path or len(path) > 1000 or path.startswith("/") or "\0" in path or ".." in path:
        raise PathError("Invalid base path.")
    if ":" in path:
        raise PathError("Invalid base path: ':' is not allowed.")
    if not path.endswith(".base"):
        raise PathError("Base paths are vault-relative and end in .base.")
    for seg in re.split(r"[/\\]", path):
        if seg == "":
            raise PathError("Invalid base path: empty segment.")
        if seg.startswith("."):
            raise PathError("Dot-folders and hidden files are not available.")
    return path


def is_base_path(path: str) -> bool:
    try:
        validate_base_path(path)
        return True
    except PathError:
        return False


def validate_folder(folder: str) -> str:
    folder = folder.strip().strip("/")
    if folder and (len(folder) > 1000 or "\0" in folder or ".." in folder or ":" in folder or folder.startswith(".") or "/." in folder or "\\" in folder):
        raise PathError("Invalid folder.")
    return folder


def _norm(folder: str) -> str:
    return nfc(folder.strip().strip("/")).casefold()


@dataclass(frozen=True)
class FolderPolicy:
    """Which folders may be read. `readable` empty means the whole vault. `blocked` always wins.
    Matching is on whole path segments, NFC-normalised and case-insensitive (Obsidian vaults are case-insensitive
    on most platforms, and a fail-closed comparison is the safe direction)."""

    readable: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()

    @classmethod
    def build(cls, readable: list[str], blocked: list[str]) -> FolderPolicy:
        return cls(tuple(_norm(f) for f in readable if f.strip().strip("/")), tuple(_norm(f) for f in blocked if f.strip().strip("/")))

    @staticmethod
    def _inside(path: str, folder: str) -> bool:
        return path == folder or path.startswith(folder + "/")

    def allows_path(self, note_path: str) -> bool:
        p = nfc(note_path).casefold()
        if any(self._inside(p, b) for b in self.blocked):
            return False
        return not self.readable or any(self._inside(p, r) for r in self.readable)

    def folder_visible(self, folder: str) -> bool:
        """A folder may be listed if it is inside a readable folder, or is an ancestor of one; never inside a blocked one."""
        f = _norm(folder)
        if f and any(self._inside(f, b) for b in self.blocked):
            return False
        if not self.readable or not f:
            return True
        return any(self._inside(f, r) or self._inside(r, f) for r in self.readable)

    def blocked_terms(self) -> list[str]:
        return list(self.blocked)

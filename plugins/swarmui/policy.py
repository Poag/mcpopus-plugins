"""What the plugin will ask SwarmUI to do. Enforced here, server side, before any request is sent."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
PARAM_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
MODEL_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f<>|\\:,\[\]{}\"']{1,200}$")  # a model name that is safe to put inside a <lora:...> tag
IMAGE_PATH_RE = re.compile(r"^(?:View/)?[A-Za-z0-9 _.,()\[\]+@#'=~-]+(?:/[A-Za-z0-9 _.,()\[\]+@#'=~-]+)*$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def norm_glob(pattern: str) -> str:
    p = pattern.strip()
    if not p or len(p) > 128 or _CTRL.search(p):
        raise ValueError("patterns are 1 to 128 characters with no control characters, for example SDXL/*")
    return p


@dataclass(frozen=True)
class ModelPolicy:
    allow: tuple[str, ...]
    deny: tuple[str, ...]

    def visible(self, name: str) -> bool:
        """Deny always wins; an empty allow list means everything not denied."""
        n = name.casefold()
        if any(fnmatch.fnmatchcase(n, p.casefold()) for p in self.deny):
            return False
        return not self.allow or any(fnmatch.fnmatchcase(n, p.casefold()) for p in self.allow)


def check_prompt(text: object, what: str, max_chars: int, allow_syntax: bool) -> str | None:
    """Why a prompt is refused, or None. The message never repeats the prompt."""
    if not isinstance(text, str):
        return f"{what} must be text."
    if len(text) > max_chars:
        return f"{what} is longer than this connector allows ({max_chars} characters, the max_prompt_chars setting)."
    if _CTRL.search(text.replace("\n", " ").replace("\t", " ")):
        return f"{what} contains control characters."
    if "<" in text and not allow_syntax:
        return (f"{what} may not contain '<'. SwarmUI treats <...> as commands (loading models, reading files, changing settings), "
                "so they are off. Use the loras argument for LoRAs; the owner can allow them with allow_prompt_syntax.")
    return None


def safe_image_path(path: object) -> str | None:
    """A path returned by SwarmUI for a saved image, if it has the expected shape; otherwise None (it is not shown)."""
    if not isinstance(path, str) or not 1 <= len(path) <= 300 or ".." in path or path.startswith(("/", "data:")) or not IMAGE_PATH_RE.fullmatch(path):
        return None
    return path


RESERVED_PARAMS = frozenset({"session_id", "prompt", "negativeprompt", "model", "width", "height", "steps", "cfgscale", "seed", "images", "rawinput"})
HARD_DENY_PREFIXES = ("init", "mask", "controlnet", "image", "output", "video", "audio", "refiner", "donotsave", "extra_metadata", "personal")


def forbidden_param(pid: str) -> bool:
    """Parameters the plugin never passes, whatever the owner lists: image inputs, output locations, video and audio, reserved names."""
    return pid in RESERVED_PARAMS or pid.startswith(HARD_DENY_PREFIXES)

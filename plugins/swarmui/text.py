"""Bounding and cleaning of text that came from SwarmUI (model descriptions, prompts in history, backend messages)."""

from __future__ import annotations

import json
import re
from typing import Any

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BIDI = re.compile("[‪-‮⁦-⁩​-‏﻿]")


def clean(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    text = re.sub(r"\s+", " ", _BIDI.sub("", _CTRL.sub(" ", text))).strip()
    return text[:limit] + ("…" if len(text) > limit else "")

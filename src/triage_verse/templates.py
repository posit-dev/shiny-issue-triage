"""Load, validate, and render executor comment templates."""

from __future__ import annotations

import pathlib
import string

TEMPLATE_NAMES = (
    "close-completed",
    "close-not-planned",
    "close-duplicate",
    "close-duplicate-cross-repo",
)
ALLOWED_PLACEHOLDERS = frozenset({"canonical_url"})
DEFAULT_DIR = "config/templates"


class TemplateError(ValueError):
    pass


def load(base_dir: str | pathlib.Path) -> dict[str, str]:
    base = pathlib.Path(base_dir)
    loaded: dict[str, str] = {}
    for name in TEMPLATE_NAMES:
        path = base / f"{name}.md"
        if not path.exists():
            raise TemplateError(f"missing template: {path}")
        text = path.read_text(encoding="utf-8")
        for field in _fields(text):
            if field not in ALLOWED_PLACEHOLDERS:
                raise TemplateError(f"{path}: unknown placeholder {{{field}}}")
        loaded[name] = text
    return loaded


def _fields(text: str) -> list[str]:
    return [f for _, f, _, _ in string.Formatter().parse(text) if f]


def render(loaded: dict[str, str], name: str, **values: str) -> str:
    return loaded[name].format(**values).strip() + "\n"

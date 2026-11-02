"""Findings of the end-to-end browser check (CSP and form behaviour in Chromium)."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def test_pattern_attributes_compile_with_the_v_flag():
    """Browsers compile pattern= with the RegExp v flag, where a bare "-" at the end of a
    character class is a syntax error: the browser then drops the check silently."""
    found = 0
    for path in TEMPLATES.rglob("*.html"):
        for pattern in re.findall(r'pattern="([^"]*)"', path.read_text()):
            found += 1
            for cls in re.findall(r"\[((?:\\.|[^\]\\])*)\]", pattern):
                assert not re.search(r"(?<!\\)-$|^-", cls), f"{path.name}: {pattern}"
    assert found >= 2

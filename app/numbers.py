"""Numbers as people type them in the forms, on a Dutch keyboard.

One rule for every form (the settings fields and the order amount): a decimal comma or point is
fine ('2,5', '2.5'), but a value whose separator could also group thousands ('1.000', '2,500',
'1.000,50') is refused rather than guessed. A neutral module (no web or database imports), so
app.settings_store and app.main share it.
"""

from __future__ import annotations

import re

SPACES = (" ", " ", " ")
_NUMBER_RE = re.compile(r"([+-]?)(\d*)(?:([.,])(\d*))?")


class AmbiguousNumber(ValueError):
    """'1.000' is a thousand to a Dutch reader and one to an English one."""


def strip_spaces(text: object) -> str:
    out = str(text if text is not None else "").strip()
    for space in SPACES:
        out = out.replace(space, "")
    return out


def split_decimal(text: str) -> tuple[str, str, str, str]:
    """(sign, whole, separator, fraction) of a typed number without spaces: '-2,50' ->
    ('-', '2', ',', '50'). Raises AmbiguousNumber when a separator may group thousands and
    ValueError('geen geldig getal') for anything that is not a plain number ('1e3', 'inf')."""
    if text.count(",") + text.count(".") > 1:
        raise AmbiguousNumber(text)
    match = _NUMBER_RE.fullmatch(text)
    if match is None or not (match.group(2) or match.group(4)):
        raise ValueError("geen geldig getal")
    sign, whole, separator, frac = match.groups()
    whole, separator, frac = whole or "", separator or "", frac or ""
    if separator and len(frac) == 3 and whole.lstrip("0"):
        raise AmbiguousNumber(text)
    return sign, whole, separator, frac


def ambiguous_nl(text: str, example: str) -> str:
    return (
        f"'{text[:20]}' is onduidelijk, want een punt of komma kan ook duizendtallen scheiden. "
        f"Schrijf het getal zonder scheidingsteken voor duizendtallen, bijvoorbeeld {example}"
    )

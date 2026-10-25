"""Helpers for data embedded in generated HTML pages."""

from __future__ import annotations

import json

# Inside a <script> element the HTML tokenizer, not JavaScript, decides where the element
# ends. Escaping only "</" is not enough: "<!--" followed by "<script" switches it to the
# "script data double escaped" state, where the real "</script>" no longer closes the
# block. JSON allows any character as a \uXXXX escape, so none of these reach the
# tokenizer. U+2028/U+2029 are escaped too, for engines that predate JSON superset.
_SCRIPT_ESCAPES = str.maketrans(
    {
        "<": "\\u003c",
        ">": "\\u003e",
        "&": "\\u0026",
        " ": "\\u2028",
        " ": "\\u2029",
    }
)


def json_for_script(value: object, *, compact: bool = True) -> str:
    """Serialise ``value`` as JSON that can be placed verbatim inside a <script> element."""
    separators = (",", ":") if compact else None
    return json.dumps(value, separators=separators).translate(_SCRIPT_ESCAPES)

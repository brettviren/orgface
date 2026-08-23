# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Serialising Python values to Elisp and reading results back.

``emacsclient --eval`` returns the printed (``prin1``) form of the result.
Rather than reverse-engineer Emacs' printer we have the daemon hand back a
base64-encoded UTF-8 JSON payload, so the only thing to strip on this side
is one layer of Elisp string quoting.
"""

from __future__ import annotations

import base64
import json
from typing import Any

# Characters that must be escaped inside an Elisp string literal.
_STRING_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
    "\f": "\\f",
    "\v": "\\v",
    "\x00": "\\0",
}


def quote_string(text: str) -> str:
    """Return an Elisp string literal denoting ``text``."""
    out = [_STRING_ESCAPES.get(ch, ch) for ch in text]
    return '"' + "".join(out) + '"'


def quote(value: Any) -> str:
    """Serialise a simple Python value as an Elisp expression."""
    if value is None:
        return "nil"
    if value is True:
        return "t"
    if value is False:
        return "nil"
    if isinstance(value, str):
        return quote_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "(list " + " ".join(quote(item) for item in value) + ")"
    raise TypeError(f"cannot serialise {type(value).__name__} to Elisp")


def call(function: str, *args: Any) -> str:
    """Build an Elisp form calling ``function`` with ``args``."""
    parts = " ".join(quote(arg) for arg in args)
    return f"({function}{' ' + parts if parts else ''})"


class ElispError(Exception):
    """The daemon returned something we could not interpret."""


def unquote_string(printed: str) -> str:
    """Undo one layer of Elisp string printing.

    ``emacsclient --eval`` prints a string result surrounded by double
    quotes with backslash escapes.  Only the escapes Emacs' printer can
    emit for our base64 payload need handling, but the full set is cheap.
    """
    text = printed.strip()
    if len(text) < 2 or not text.startswith('"') or not text.endswith('"'):
        raise ElispError(f"expected a quoted string, got: {printed.strip()!r}")
    body = text[1:-1]

    simple = {
        "n": "\n",
        "t": "\t",
        "r": "\r",
        "f": "\f",
        "v": "\v",
        "e": "\x1b",
        "a": "\a",
        "b": "\b",
        "d": "\x7f",
        "s": " ",
        '"': '"',
        "\\": "\\",
    }

    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise ElispError("trailing backslash in Elisp string")
        code = body[index]
        index += 1
        if code in simple:
            out.append(simple[code])
        elif code == "\n":  # escaped newline: line continuation
            continue
        elif code == "x":
            digits = ""
            while index < len(body) and body[index] in "0123456789abcdefABCDEF":
                digits += body[index]
                index += 1
            out.append(chr(int(digits, 16)) if digits else "x")
        elif code in "01234567":
            digits = code
            while index < len(body) and len(digits) < 3 and body[index] in "01234567":
                digits += body[index]
                index += 1
            out.append(chr(int(digits, 8)))
        else:
            out.append(code)
    return "".join(out)


def decode_payload(printed: str) -> Any:
    """Decode a base64 JSON payload returned by the daemon."""
    encoded = unquote_string(printed)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ElispError(f"result was not valid base64: {exc}") from exc
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ElispError(f"result was not valid JSON: {exc}") from exc

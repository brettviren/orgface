# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Elisp serialisation and result decoding."""

from __future__ import annotations

import base64
import json

import pytest

from orgface.elisp import (
    ElispError,
    call,
    decode_payload,
    quote,
    quote_string,
    unquote_string,
)


class TestQuote:
    def test_plain_string(self):
        assert quote_string("hello") == '"hello"'

    def test_escapes(self):
        assert quote_string('a"b\\c\nd') == '"a\\"b\\\\c\\nd"'

    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, "nil"),
            (True, "t"),
            (False, "nil"),
            (7, "7"),
            ("x", '"x"'),
            (["a", 1], '(list "a" 1)'),
        ],
    )
    def test_values(self, value, expected):
        assert quote(value) == expected

    def test_unsupported(self):
        with pytest.raises(TypeError):
            quote({"a": 1})

    def test_call(self):
        assert call("f", "x", 2) == '(f "x" 2)'

    def test_call_without_arguments(self):
        assert call("f") == "(f)"


class TestUnquoteString:
    def test_round_trip(self):
        assert unquote_string('"hello"') == "hello"

    def test_escapes(self):
        assert unquote_string('"a\\"b\\\\c\\nd"') == 'a"b\\c\nd'

    def test_literal_newline_survives(self):
        """Emacs prints newlines literally unless told otherwise."""
        assert unquote_string('"a\nb"') == "a\nb"

    @pytest.mark.parametrize("printed", ["nil", "", "hello", '"unterminated'])
    def test_non_strings_are_rejected(self, printed):
        with pytest.raises(ElispError):
            unquote_string(printed)


class TestDecodePayload:
    def _printed(self, obj) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return '"' + base64.b64encode(raw).decode("ascii") + '"\n'

    def test_decodes_json(self):
        payload = {"roam_directory": "/home/u/org/roam", "templates": []}
        assert decode_payload(self._printed(payload)) == payload

    def test_decodes_non_ascii(self):
        payload = {"title": "日本 2026"}
        assert decode_payload(self._printed(payload)) == payload

    def test_bad_base64(self):
        with pytest.raises(ElispError, match="base64"):
            decode_payload('"not base64!!"')

    def test_bad_json(self):
        encoded = base64.b64encode(b"{nope").decode("ascii")
        with pytest.raises(ElispError, match="JSON"):
            decode_payload(f'"{encoded}"')

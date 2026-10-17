# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The ``[slurp.NAME]`` and ``[orger.NAME]`` tables of the config file.

A source declares its own settings, so the configuration module can only
check a table when the source is used.  These tests use settings declared
here, standing in for any source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orgface.config import (
    ConfigError,
    load,
    parse_bool,
    parse_path,
    parse_str,
    resolve_section,
    table_lines,
)
from orgface.sources import setting

SETTINGS = (
    setting("slurp", "demo", "token", None, parse_str, "A secret.", secret=True),
    setting("slurp", "demo", "directory", None, parse_path, "Where."),
    setting("slurp", "demo", "verbose", False, parse_bool, "Chatty."),
)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestDeclaration:
    def test_table_and_environment_names_follow_the_source(self):
        token = SETTINGS[0]
        assert token.section == "slurp.demo"
        assert token.key == "token"
        assert token.env == "ORGFACE_SLURP_DEMO_TOKEN"

    def test_dashes_in_a_source_name_become_underscores_in_the_environment(self):
        assert setting("orger", "my-src", "key", None, parse_str, "").env == "ORGFACE_ORGER_MY_SRC_KEY"


class TestResolve:
    def test_defaults_when_nothing_is_configured(self):
        config = load(environ={})
        assert resolve_section(config, "slurp.demo", SETTINGS, environ={}) == {
            "token": None,
            "directory": None,
            "verbose": False,
        }

    def test_the_file_beats_the_environment_and_the_command_line_beats_both(self, tmp_path):
        config = load(
            path=write(tmp_path / "c.toml", '[slurp.demo]\ndirectory = "/from/file"\n'),
            environ={},
        )
        env = {"ORGFACE_SLURP_DEMO_DIRECTORY": "/from/env", "ORGFACE_SLURP_DEMO_VERBOSE": "yes"}
        resolved = resolve_section(config, "slurp.demo", SETTINGS, environ=env)
        assert resolved["directory"] == Path("/from/file")
        assert resolved["verbose"] is True  # only the environment said so

        resolved = resolve_section(
            config, "slurp.demo", SETTINGS, {"directory": Path("/from/cli"), "verbose": None}, env
        )
        assert resolved["directory"] == Path("/from/cli")
        assert resolved["verbose"] is True  # an unset option does not clobber

    def test_a_host_file_changes_one_value_and_inherits_the_rest(self, tmp_path):
        base = write(
            tmp_path / "config.toml",
            '[slurp.demo]\ndirectory = "/base"\nverbose = true\n',
        )
        write(tmp_path / "elsewhere.toml", '[slurp.demo]\ndirectory = "/host"\n')
        config = load(path=base, environ={"ORGFACE_HOST": "elsewhere"})
        resolved = resolve_section(config, "slurp.demo", SETTINGS, environ={})
        assert resolved["directory"] == Path("/host")
        assert resolved["verbose"] is True

    def test_slurp_and_orger_tables_of_one_name_are_separate(self, tmp_path):
        config = load(
            path=write(
                tmp_path / "c.toml",
                '[slurp.demo]\ndirectory = "/s"\n[orger.demo]\noutput = "o.org"\n',
            ),
            environ={},
        )
        assert set(config.source_tables) == {"slurp.demo", "orger.demo"}

    def test_a_misspelt_key_is_an_error_that_names_the_choices(self, tmp_path):
        config = load(path=write(tmp_path / "c.toml", "[slurp.demo]\ndirecory = 1\n"), environ={})
        with pytest.raises(ConfigError, match=r"\[slurp.demo\]: unknown key 'direcory'.*directory"):
            resolve_section(config, "slurp.demo", SETTINGS, environ={})

    def test_a_bad_value_names_the_setting(self, tmp_path):
        config = load(path=write(tmp_path / "c.toml", '[slurp.demo]\nverbose = "perhaps"\n'), environ={})
        with pytest.raises(ConfigError, match=r"slurp.demo.verbose"):
            resolve_section(config, "slurp.demo", SETTINGS, environ={})

    def test_a_table_of_a_source_nobody_asks_about_is_not_checked(self, tmp_path):
        # A config shared between machines may configure what this one lacks.
        config = load(path=write(tmp_path / "c.toml", "[slurp.absent]\nanything = 1\n"), environ={})
        assert "slurp.absent" in config.source_tables

    @pytest.mark.parametrize("text", ["slurp = 1\n", "[slurp]\ndemo = 1\n"])
    def test_a_malformed_table_is_rejected_at_load(self, tmp_path, text):
        with pytest.raises(ConfigError, match="must be a table"):
            load(path=write(tmp_path / "c.toml", text), environ={})


class TestShow:
    def test_secrets_are_withheld_unless_asked_for(self):
        values = {"token": "sekrit", "directory": Path("/d"), "verbose": False}
        shown = "\n".join(table_lines("[slurp.demo]", SETTINGS, values))
        assert "sekrit" not in shown
        assert "(set, withheld)" in shown
        assert 'directory = "/d"' in shown
        revealed = "\n".join(table_lines("[slurp.demo]", SETTINGS, values, secrets=True))
        assert 'token = "sekrit"' in revealed

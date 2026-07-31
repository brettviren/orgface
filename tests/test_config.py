"""Layered configuration resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from org_cli_helper.config import ConfigError, load, parse_duration


class TestParseDuration:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1h", 3600),
            ("30m", 1800),
            ("90s", 90),
            ("45", 45),
            (600, 600),
            ("1h30m", 5400),
            ("2 hours", 7200),
            ("1d", 86400),
        ],
    )
    def test_accepted(self, value, expected):
        assert parse_duration(value) == expected

    @pytest.mark.parametrize("value", ["never", "none", "inf", "forever", "0", 0])
    def test_no_expiry(self, value):
        assert parse_duration(value) is None

    @pytest.mark.parametrize("value", ["", "1 fortnight", "abc", "-5", "1h!"])
    def test_rejected(self, value):
        with pytest.raises(ConfigError):
            parse_duration(value)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point XDG at a scratch tree so the real config is never read."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


class TestLoad:
    def test_defaults(self, isolated):
        config = load(environ={})
        assert config.server_name == "org-cli-helper"
        assert config.lifetime == 3600
        assert config.emacs == "emacs"
        assert config.source is None

    def test_config_file_overrides_environment(self, isolated):
        """CLAUDE.md fixes the order as env -> file -> command line."""
        path = isolated / "custom.toml"
        path.write_text('[daemon]\nserver_name = "from-file"\n', encoding="utf-8")
        config = load(
            path=path, environ={"ORG_CLI_HELPER_SERVER_NAME": "from-env"}
        )
        assert config.server_name == "from-file"
        assert config.source == path

    def test_environment_is_used_when_the_file_is_silent(self, isolated):
        path = isolated / "custom.toml"
        path.write_text('[daemon]\nlifetime = "5m"\n', encoding="utf-8")
        config = load(
            path=path, environ={"ORG_CLI_HELPER_SERVER_NAME": "from-env"}
        )
        assert config.server_name == "from-env"
        assert config.lifetime == 300

    def test_overrides_win(self, isolated):
        path = isolated / "custom.toml"
        path.write_text('[daemon]\nserver_name = "from-file"\n', encoding="utf-8")
        config = load(
            path=path,
            overrides={"server_name": "from-cli"},
            environ={"ORG_CLI_HELPER_SERVER_NAME": "from-env"},
        )
        assert config.server_name == "from-cli"

    def test_unset_overrides_do_not_clobber(self, isolated):
        path = isolated / "custom.toml"
        path.write_text('[daemon]\nserver_name = "from-file"\n', encoding="utf-8")
        config = load(
            path=path, overrides={"server_name": None, "lifetime": None}, environ={}
        )
        assert config.server_name == "from-file"
        assert config.lifetime == 3600

    def test_xdg_config_is_found(self, isolated):
        target = isolated / "config" / "org-cli-helper" / "config.toml"
        target.parent.mkdir(parents=True)
        target.write_text('[daemon]\nlifetime = "15m"\n', encoding="utf-8")
        config = load(environ={})
        assert config.lifetime == 900
        assert config.source == target

    def test_paths_are_expanded(self, isolated, monkeypatch):
        monkeypatch.setenv("HOME", str(isolated))
        path = isolated / "custom.toml"
        path.write_text('[roam]\ndirectory = "~/org/roam"\n', encoding="utf-8")
        config = load(path=path, environ={})
        assert config.roam_directory == Path(isolated) / "org" / "roam"

    def test_never_lifetime(self, isolated):
        path = isolated / "custom.toml"
        path.write_text('[daemon]\nlifetime = "never"\n', encoding="utf-8")
        assert load(path=path, environ={}).lifetime is None

    def test_unknown_section_is_reported(self, isolated):
        path = isolated / "custom.toml"
        path.write_text('[nope]\nx = 1\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="unknown section"):
            load(path=path, environ={})

    def test_unknown_key_is_reported(self, isolated):
        path = isolated / "custom.toml"
        path.write_text('[daemon]\nnope = 1\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="unknown key"):
            load(path=path, environ={})

    def test_malformed_toml_is_reported(self, isolated):
        path = isolated / "custom.toml"
        path.write_text("[daemon\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load(path=path, environ={})

    def test_missing_explicit_file(self, isolated):
        with pytest.raises(ConfigError, match="no such config file"):
            load(path=isolated / "absent.toml", environ={})

    def test_bad_value_names_its_setting(self, isolated):
        path = isolated / "custom.toml"
        path.write_text('[daemon]\nlifetime = "banana"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="daemon.lifetime"):
            load(path=path, environ={})

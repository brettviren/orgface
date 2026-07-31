"""Command line surface: parsing, help, and the paths that avoid Emacs.

Anything needing a live daemon lives in test_emacs_live.py.  Here the
daemon is kept out of reach by pointing roam.directory at a scratch tree,
which is the documented way to run ``log`` without Emacs.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from org_cli_helper.cli import main

NOTE = """\
:PROPERTIES:
:ID:       068075f1-de9f-4679-b5e5-ad0ab32d4805
:END:
#+title: 2026-07-30
* First thing
<2026-07-30 Thu 13:32>

Body of the first thing.
* Second thing
<2026-07-30 Thu 15:17>

Body of the second thing.
"""


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A config that resolves the daily notes without consulting Emacs."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgconfig"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdgcache"))
    monkeypatch.delenv("PAGER", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    roam = tmp_path / "roam"
    (roam / "daily").mkdir(parents=True)
    (roam / "daily" / "2026-07-30.org").write_text(NOTE, encoding="utf-8")

    config = tmp_path / "config.toml"
    config.write_text(
        f'[roam]\ndirectory = "{roam}"\ndailies_directory = "daily"\n'
        "[log]\npager = \"cat\"\n",
        encoding="utf-8",
    )
    return config


def run(runner, config, *args):
    return runner.invoke(main, ["-c", str(config), *args])


class TestTopLevel:
    def test_no_arguments_shows_help(self, runner):
        """Help, but a nonzero status: nothing was actually asked for."""
        result = runner.invoke(main, [])
        assert result.exit_code != 0
        assert "Usage:" in result.output
        assert "commit" in result.output and "log" in result.output

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_help_flags(self, runner, flag):
        assert runner.invoke(main, [flag]).exit_code == 0

    def test_version(self, runner):
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "org-cli-helper" in result.output

    def test_missing_config_file_is_reported(self, runner, tmp_path):
        result = runner.invoke(main, ["-c", str(tmp_path / "nope.toml"), "log"])
        assert result.exit_code != 0

    def test_bad_config_is_reported(self, runner, tmp_path):
        bad = tmp_path / "bad.toml"
        bad.write_text('[daemon]\nlifetime = "banana"\n', encoding="utf-8")
        result = runner.invoke(main, ["-c", str(bad), "config", "show"])
        assert result.exit_code != 0
        assert "daemon.lifetime" in result.output


class TestLog:
    def test_default_lists_newest_first(self, runner, workspace):
        result = run(runner, workspace, "log", "--no-pager")
        assert result.exit_code == 0
        assert result.output.index("Second thing") < result.output.index("First thing")

    def test_oneline(self, runner, workspace):
        result = run(runner, workspace, "log", "--oneline", "--no-pager")
        assert result.exit_code == 0
        assert len(result.output.strip().splitlines()) == 2
        assert "068075f1#1" in result.output

    def test_reverse(self, runner, workspace):
        result = run(runner, workspace, "log", "--reverse", "--no-pager")
        assert result.output.index("First thing") < result.output.index("Second thing")

    def test_max_count(self, runner, workspace):
        result = run(runner, workspace, "log", "-n", "1", "--oneline", "--no-pager")
        assert len(result.output.strip().splitlines()) == 1

    def test_since_filters(self, runner, workspace):
        result = run(runner, workspace, "log", "--since", "2026-07-31", "--no-pager")
        assert result.exit_code == 1
        assert "no entries" in result.output

    def test_after_is_a_synonym_for_since(self, runner, workspace):
        one = run(runner, workspace, "log", "--since", "2026-07-30", "--oneline", "--no-pager")
        two = run(runner, workspace, "log", "--after", "2026-07-30", "--oneline", "--no-pager")
        assert one.output == two.output

    def test_before_is_a_synonym_for_until(self, runner, workspace):
        one = run(runner, workspace, "log", "--until", "2026-07-30", "--oneline", "--no-pager")
        two = run(runner, workspace, "log", "--before", "2026-07-30", "--oneline", "--no-pager")
        assert one.output == two.output

    def test_until_includes_the_whole_named_day(self, runner, workspace):
        result = run(
            runner, workspace, "log", "--until", "2026-07-30", "--oneline", "--no-pager"
        )
        assert len(result.output.strip().splitlines()) == 2

    def test_today_conflicts_with_since(self, runner, workspace):
        result = run(runner, workspace, "log", "--today", "--since", "2026-01-01")
        assert result.exit_code != 0
        assert "cannot be combined" in result.output

    def test_bad_date_is_reported(self, runner, workspace):
        result = run(runner, workspace, "log", "--since", "next tuesday")
        assert result.exit_code != 0
        assert "unrecognised date" in result.output

    def test_missing_dailies_directory_is_reported(self, runner, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text(
            f'[roam]\ndirectory = "{tmp_path / "absent"}"\ndailies_directory = "daily"\n',
            encoding="utf-8",
        )
        result = run(runner, config, "log", "--no-pager")
        assert result.exit_code != 0
        assert "no such directory" in result.output

    def test_no_colour_when_not_a_terminal(self, runner, workspace):
        result = run(runner, workspace, "log", "--no-pager")
        assert "\x1b[" not in result.output

    def test_colour_always(self, runner, workspace):
        result = run(runner, workspace, "log", "--color", "always", "--no-pager")
        assert "\x1b[" in result.output


class TestCommitArguments:
    """Argument handling that stops before any daemon contact."""

    def test_dry_run_needs_a_message(self, runner, workspace):
        result = run(runner, workspace, "commit", "--dry-run")
        assert result.exit_code != 0
        assert "--dry-run needs at least one -m" in result.output

    def test_help_does_not_touch_emacs(self, runner, workspace):
        result = run(runner, workspace, "commit", "-h")
        assert result.exit_code == 0
        assert "--template" in result.output
        assert "-m" in result.output


class TestConfigCommand:
    def test_show_reports_resolved_values(self, runner, workspace):
        result = run(runner, workspace, "config", "show")
        assert result.exit_code == 0
        assert "daemon.server_name = org-cli-helper" in result.output
        assert "roam.dailies_directory = daily" in result.output

    def test_show_env_names(self, runner, workspace):
        result = run(runner, workspace, "config", "show", "--env")
        assert "ORG_CLI_HELPER_SERVER_NAME" in result.output

    def test_paths(self, runner, workspace):
        result = run(runner, workspace, "config", "paths")
        assert result.exit_code == 0
        assert "org-cli-helper" in result.output

    def test_no_args_shows_help(self, runner, workspace):
        result = run(runner, workspace, "config")
        assert result.exit_code != 0
        assert "Usage:" in result.output


class TestDaemonCommand:
    def test_no_args_shows_help(self, runner, workspace):
        result = run(runner, workspace, "daemon")
        assert result.exit_code != 0
        assert "start" in result.output and "stop" in result.output

    def test_overrides_reach_the_config(self, runner, workspace):
        result = run(
            runner, workspace, "-s", "other-server", "config", "show"
        )
        assert "daemon.server_name = other-server" in result.output

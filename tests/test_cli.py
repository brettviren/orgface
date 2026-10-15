# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command line surface: parsing, help, and the paths that avoid Emacs.

Anything needing a live daemon lives in test_emacs_live.py.  Here the
daemon is kept out of reach by pointing roam.directory at a scratch tree,
which is the documented way to run ``log`` without Emacs.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from click.testing import CliRunner

from orgface.cli import main

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
        assert "orgface" in result.output

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


class TestNote:
    """Everything up to the capture, plus the capture call itself."""

    @pytest.fixture
    def captured(self, monkeypatch):
        """Record what would have been handed to the daemon."""
        calls = []

        def note(self, topic, body):
            calls.append((topic, body))
            return {"topic": topic, "file": "/roam/20260804-context_notes.org"}

        monkeypatch.setattr("orgface.emacs.Emacs.note", note)
        return calls

    def test_dry_run_needs_a_message(self, runner, workspace):
        result = run(runner, workspace, "note", "--dry-run")
        assert result.exit_code != 0
        assert "--dry-run needs at least one -m" in result.output

    def test_dry_run_shows_the_context(self, runner, workspace, tmp_path, monkeypatch):
        work = tmp_path / "some-repo" / "src"
        (tmp_path / "some-repo" / ".git").mkdir(parents=True)
        work.mkdir(parents=True)
        monkeypatch.chdir(work)

        result = run(runner, workspace, "note", "--dry-run", "-m", "Fixed the thing")
        assert result.exit_code == 0
        assert "* Fixed the thing :some_repo:" in result.output
        assert f":DIRECTORY: {work.resolve()}" in result.output
        assert ":HOST:" in result.output and ":USER:" in result.output
        # An org timestamp, to the second, on its own line.
        assert re.search(r"^\[\d{4}-\d\d-\d\d \w{3} \d\d:\d\d:\d\d\]$",
                         result.output, re.MULTILINE)

    def test_empty_title_is_refused(self, runner, workspace):
        result = run(runner, workspace, "note", "-m", "   ")
        assert result.exit_code != 0
        assert "must not be empty" in result.output

    def test_captures_to_the_configured_topic(self, runner, workspace, captured):
        result = run(runner, workspace, "note", "-m", "Fixed the thing")
        assert result.exit_code == 0
        assert [topic for topic, _ in captured] == ["Context Notes"]
        assert "* Fixed the thing" in captured[0][1]
        assert "[Context Notes] Fixed the thing" in result.output
        assert "1 entry added to" in result.output

    def test_topic_option_wins(self, runner, workspace, captured):
        result = run(runner, workspace, "note", "-T", "Other Topic", "-m", "Hi")
        assert result.exit_code == 0
        assert captured[0][0] == "Other Topic"
        assert "[Other Topic] Hi" in result.output

    def test_configured_topic_is_used(self, runner, workspace, captured, tmp_path):
        config = tmp_path / "with-topic.toml"
        config.write_text(
            workspace.read_text(encoding="utf-8") + '[note]\ntopic = "Journal"\n',
            encoding="utf-8",
        )
        assert run(runner, config, "note", "-m", "Hi").exit_code == 0
        assert captured[0][0] == "Journal"

    def test_quiet_says_nothing(self, runner, workspace, captured):
        result = run(runner, workspace, "note", "-q", "-m", "Fixed the thing")
        assert result.exit_code == 0
        assert result.output == ""

    def test_tags_are_reported(
        self, runner, workspace, captured, tmp_path, monkeypatch
    ):
        (tmp_path / "some-repo" / ".git").mkdir(parents=True)
        monkeypatch.chdir(tmp_path / "some-repo")
        result = run(runner, workspace, "note", "-m", "Fixed the thing")
        assert "tags: :some_repo:" in result.output

    def test_help_does_not_touch_emacs(self, runner, workspace):
        result = run(runner, workspace, "note", "-h")
        assert result.exit_code == 0
        assert "--topic" in result.output and "-m" in result.output


class TestConfigCommand:
    def test_show_reports_resolved_values(self, runner, workspace):
        result = run(runner, workspace, "config", "show")
        assert result.exit_code == 0
        assert 'server_name = "orgface"' in result.output
        assert 'dailies_directory = "daily"' in result.output
        assert 'topic = "Context Notes"' in result.output

    def test_show_output_is_loadable_toml(self, runner, workspace, tmp_path):
        for args in (("config", "show"), ("config", "show", "--env")):
            result = run(runner, workspace, *args)
            saved = tmp_path / "saved.toml"
            saved.write_text(result.output, encoding="utf-8")
            again = run(runner, saved, *args)
            assert again.exit_code == 0, again.output
            assert again.output.splitlines()[1:] == result.output.splitlines()[1:]

    def test_init_creates_default_config(self, runner, workspace, tmp_path):
        target = tmp_path / "xdgconfig" / "orgface" / "config.toml"
        assert not target.exists()
        result = runner.invoke(main, ["config", "init"])
        assert result.exit_code == 0, result.output
        assert target.exists()
        loaded = runner.invoke(main, ["-c", str(target), "config", "show"])
        assert loaded.exit_code == 0, loaded.output

    def test_init_leaves_existing_config_alone(self, runner, workspace, tmp_path):
        target = tmp_path / "xdgconfig" / "orgface" / "config.toml"
        target.parent.mkdir(parents=True)
        target.write_text('[note]\ntopic = "Mine"\n', encoding="utf-8")
        result = runner.invoke(main, ["config", "init"])
        assert result.exit_code == 0
        assert "already exists" in result.output
        assert target.read_text(encoding="utf-8") == '[note]\ntopic = "Mine"\n'

    def test_show_env_names(self, runner, workspace):
        result = run(runner, workspace, "config", "show", "--env")
        assert "ORGFACE_SERVER_NAME" in result.output

    def test_paths(self, runner, workspace):
        result = run(runner, workspace, "config", "paths")
        assert result.exit_code == 0
        assert "orgface" in result.output

    def test_no_args_shows_help(self, runner, workspace):
        result = run(runner, workspace, "config")
        assert result.exit_code != 0
        assert "Usage:" in result.output


class TestVisit:
    def test_visits_an_existing_note(self, runner, workspace, tmp_path, monkeypatch):
        visited = []
        monkeypatch.setattr(
            "orgface.emacs.Emacs.visit", lambda self, path: visited.append(path)
        )
        result = run(runner, workspace, "visit", "2026-07-30")
        assert result.exit_code == 0
        assert visited == [tmp_path / "roam" / "daily" / "2026-07-30.org"]

    def test_defaults_to_today(self, runner, workspace, tmp_path, monkeypatch):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 7, 30)

        monkeypatch.setattr("orgface.commands.visit.datetime", FixedDatetime)
        visited = []
        monkeypatch.setattr(
            "orgface.emacs.Emacs.visit", lambda self, path: visited.append(path)
        )
        result = run(runner, workspace, "visit")
        assert result.exit_code == 0
        assert visited == [tmp_path / "roam" / "daily" / "2026-07-30.org"]

    def test_missing_daily_note_is_reported(self, runner, workspace):
        result = run(runner, workspace, "visit", "2026-08-01")
        assert result.exit_code == 1
        assert "no daily note for 2026-08-01" in result.output

    def test_bad_date_is_reported(self, runner, workspace):
        result = run(runner, workspace, "visit", "next tuesday")
        assert result.exit_code != 0
        assert "unrecognised date" in result.output

    def test_help_does_not_touch_emacs(self, runner, workspace):
        result = run(runner, workspace, "visit", "-h")
        assert result.exit_code == 0
        assert "DATE" in result.output
        assert "--topic" in result.output

    def test_a_multi_word_date_needs_no_quoting(self, runner, workspace, monkeypatch):
        monkeypatch.setattr(
            "orgface.emacs.Emacs.visit", lambda self, path: None
        )
        result = run(runner, workspace, "visit", "3", "days", "ago")
        assert result.exit_code == 1
        assert "no daily note for" in result.output


class TestVisitTopic:
    @pytest.fixture
    def topics(self, monkeypatch, tmp_path):
        """An org-roam database holding one topic."""
        note = tmp_path / "roam" / "20260804-context_notes.org"
        note.write_text("#+title: Context Notes\n", encoding="utf-8")
        known = {"Context Notes": str(note)}
        monkeypatch.setattr(
            "orgface.emacs.Emacs.topic",
            lambda self, title: {"topic": title, "file": known.get(title)},
        )
        return note

    @pytest.fixture
    def visited(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "orgface.emacs.Emacs.visit", lambda self, path: seen.append(path)
        )
        return seen

    def test_defaults_to_the_configured_topic(self, runner, workspace, topics, visited):
        result = run(runner, workspace, "visit", "-T")
        assert result.exit_code == 0
        assert visited == [topics]

    def test_arguments_name_the_topic(self, runner, workspace, topics, visited):
        result = run(runner, workspace, "visit", "-T", "Context", "Notes")
        assert result.exit_code == 0
        assert visited == [topics]

    def test_a_date_is_not_interpreted(self, runner, workspace, topics, visited):
        """With -T, 'today' is a title and nothing else."""
        result = run(runner, workspace, "visit", "-T", "today")
        assert result.exit_code == 1
        assert "no org-roam note titled 'today'" in result.output
        assert visited == []

    def test_a_missing_topic_is_reported(self, runner, workspace, topics, visited):
        result = run(runner, workspace, "visit", "-T", "Nothing Here")
        assert result.exit_code == 1
        assert "no org-roam note titled 'Nothing Here'" in result.output


class TestDaemonCommand:
    def test_no_args_shows_help(self, runner, workspace):
        result = run(runner, workspace, "daemon")
        assert result.exit_code != 0
        assert "start" in result.output and "stop" in result.output

    def test_overrides_reach_the_config(self, runner, workspace):
        result = run(
            runner, workspace, "-s", "other-server", "config", "show"
        )
        assert 'server_name = "other-server"' in result.output

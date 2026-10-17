# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The GitHub source: ``slurp github recent|archive`` and ``orger github``.

The network is the one thing not used: ghexport's call to the API is
replaced.  The archive is a real ``.tar.gz``, and the org side runs in a
separate process against real HPI and orger, because HPI keeps its
configuration in module globals that one process can only set once.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import date
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner

from orgface.cli import main
from orgface.sources.github import core


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def make_archive(path: Path, members: dict[str, bytes | None]) -> Path:
    """A ``.tar.gz`` of these members; None as a value makes a directory."""
    with tarfile.open(path, "w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return path


EXPORT = {
    "./schema.json": b"{}",
    "./issues_000001.json": b'[{"title": "t"}]',
    "./repositories/": None,
    "./repositories/a.git/HEAD": b"ref: refs/heads/main\n",
    "./repositories/nested.json": b"[]",  # JSON, but not at the top level
    "../escape.json": b"[]",
    "./notes.txt": b"not json",
}


class TestUnpackArchive:
    def test_keeps_only_the_json_at_the_top_level(self, tmp_path):
        archive = make_archive(tmp_path / "x.tar.gz", EXPORT)
        kept = core.unpack_archive(archive, tmp_path / "out")
        assert kept == ["issues_000001.json", "schema.json"]
        assert sorted(p.name for p in (tmp_path / "out").iterdir()) == kept
        assert (tmp_path / "out" / "issues_000001.json").read_bytes() == b'[{"title": "t"}]'
        assert not (tmp_path / "escape.json").exists()

    def test_leaves_no_staging_behind(self, tmp_path):
        core.unpack_archive(make_archive(tmp_path / "x.tar.gz", EXPORT), tmp_path / "out")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out", "x.tar.gz"]

    def test_replaces_the_json_already_there_and_nothing_else(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "stale_000009.json").write_text("[]")
        (out / "issues_000001.json").write_text("old")
        (out / "README").write_text("mine")
        core.unpack_archive(make_archive(tmp_path / "x.tar.gz", EXPORT), out)
        assert sorted(p.name for p in out.iterdir()) == ["README", "issues_000001.json", "schema.json"]
        assert (out / "issues_000001.json").read_text() == '[{"title": "t"}]'

    def test_an_archive_without_a_schema_changes_nothing(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "keep.json").write_text("[]")
        archive = make_archive(tmp_path / "x.tar.gz", {"./issues_000001.json": b"[]"})
        with pytest.raises(core.GithubError, match="schema.json"):
            core.unpack_archive(archive, out)
        assert [p.name for p in out.iterdir()] == ["keep.json"]

    def test_something_that_is_not_an_archive_is_reported(self, tmp_path):
        not_archive = tmp_path / "x.tar.gz"
        not_archive.write_text("plainly not")
        with pytest.raises(core.GithubError, match="x.tar.gz"):
            core.unpack_archive(not_archive, tmp_path / "out")
        assert [p.name for p in tmp_path.iterdir()] == ["x.tar.gz"]


class TestWriteDump:
    def test_names_the_file_for_the_day_and_keeps_the_data(self, tmp_path):
        written = core.write_dump({"events": [1]}, tmp_path / "d", date(2026, 10, 17))
        assert written == tmp_path / "d" / "ghexport-2026-10-17.json"
        assert json.loads(written.read_text()) == {"events": [1]}

    def test_a_second_dump_on_the_same_day_replaces_it_and_other_days_stay(self, tmp_path):
        core.write_dump({"n": 1}, tmp_path / "d", date(2026, 10, 16))
        core.write_dump({"n": 2}, tmp_path / "d", date(2026, 10, 17))
        core.write_dump({"n": 3}, tmp_path / "d", date(2026, 10, 17))
        files = sorted((tmp_path / "d").iterdir())
        assert [f.name for f in files] == ["ghexport-2026-10-16.json", "ghexport-2026-10-17.json"]
        assert json.loads(files[1].read_text()) == {"n": 3}

    def test_the_directory_holds_only_dumps_since_hpi_reads_all_of_it(self, tmp_path):
        core.write_dump({}, tmp_path / "d", date(2026, 10, 17))
        assert [p.name for p in (tmp_path / "d").iterdir()] == ["ghexport-2026-10-17.json"]
        assert [p.name for p in tmp_path.iterdir()] == ["d"]

    def test_a_failure_leaves_nothing_and_no_half_written_dump(self, tmp_path):
        with pytest.raises(TypeError):
            core.write_dump({"unserialisable": object()}, tmp_path / "d", date(2026, 10, 17))
        assert list((tmp_path / "d").iterdir()) == []
        assert [p.name for p in tmp_path.iterdir()] == ["d"]

    def test_the_file_is_as_readable_as_any_other(self, tmp_path):
        written = core.write_dump({}, tmp_path / "d", date(2026, 10, 17))
        umask = os.umask(0)
        os.umask(umask)
        assert written.stat().st_mode & 0o777 == 0o666 & ~umask


class TestSlurpArchiveCommand:
    def test_unpacks_into_the_configured_directory(self, runner, tmp_path):
        out = tmp_path / "gdpr"
        config = tmp_path / "c.toml"
        config.write_text(f'[slurp.github]\narchive_directory = "{out}"\n')
        archive = make_archive(tmp_path / "x.tar.gz", EXPORT)
        result = runner.invoke(main, ["-c", str(config), "slurp", "github", "archive", str(archive)])
        assert result.exit_code == 0, result.output
        assert "kept 2 files" in result.output
        assert (out / "schema.json").exists()

    def test_the_command_line_directory_wins(self, runner, tmp_path):
        archive = make_archive(tmp_path / "x.tar.gz", EXPORT)
        result = runner.invoke(
            main, ["slurp", "github", "archive", str(archive), "-d", str(tmp_path / "here")]
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "here" / "schema.json").exists()

    def test_the_default_is_under_the_xdg_data_directory(self, runner, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        archive = make_archive(tmp_path / "x.tar.gz", EXPORT)
        assert runner.invoke(main, ["slurp", "github", "archive", str(archive)]).exit_code == 0
        assert (tmp_path / "data/orgface/slurp/github/archive/schema.json").exists()

    def test_a_bad_archive_is_an_error_not_a_traceback(self, runner, tmp_path):
        bad = tmp_path / "x.tar.gz"
        bad.write_text("no")
        result = runner.invoke(main, ["slurp", "github", "archive", str(bad), "-d", str(tmp_path / "o")])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_no_arguments_shows_help(self, runner):
        assert "Usage:" in runner.invoke(main, ["slurp", "github", "archive"]).output


@pytest.fixture
def api(monkeypatch):
    """ghexport's call to GitHub, recording what it was asked."""
    export = pytest.importorskip("ghexport.export")
    asked: list[dict] = []

    def get_json(**kwargs):
        asked.append(kwargs)
        return {"events": [{"id": "1"}]}

    monkeypatch.setattr(export, "get_json", get_json)
    return asked


@pytest.fixture
def fake_pass(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "pass"
    script.write_text(
        '#!/bin/sh\ncase "$1" in good) echo from-pass ;; *) echo "no $1" >&2; exit 1 ;; esac\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


class TestSlurpRecentCommand:
    def run(self, runner, tmp_path, *args, config="", env=None):
        path = tmp_path / "c.toml"
        path.write_text(config)
        return runner.invoke(
            main,
            ["-c", str(path), "slurp", "github", "recent", "-d", str(tmp_path / "dumps"), *args],
            env=env,
        )

    def test_writes_the_dump_and_says_where(self, runner, tmp_path, api):
        result = self.run(runner, tmp_path, "--token", "t")
        assert result.exit_code == 0, result.output
        written = Path(result.stdout.strip())
        assert written.parent == tmp_path / "dumps"
        assert json.loads(written.read_text()) == {"events": [{"id": "1"}]}
        assert api == [{"token": "t", "include": None, "include_repos_traffic": False}]

    def test_passes_on_what_to_include(self, runner, tmp_path, api):
        self.run(runner, tmp_path, "--token", "t", "--include", "events", "--traffic")
        assert api[0]["include"] == ["events"]
        assert api[0]["include_repos_traffic"] is True

    def test_an_unknown_field_is_refused_before_asking_github(self, runner, tmp_path, api):
        result = self.run(runner, tmp_path, "--token", "t", "--include", "nonsense")
        assert result.exit_code == 1
        assert "nonsense" in result.output and "events" in result.output
        assert api == []

    def test_without_a_token_it_says_where_to_put_one(self, runner, tmp_path, api):
        result = self.run(runner, tmp_path)
        assert result.exit_code == 1
        assert "ORGFACE_SLURP_GITHUB_TOKEN" in result.output and "token_passname" in result.output
        assert api == []

    def test_token_comes_from_the_environment(self, runner, tmp_path, api):
        self.run(runner, tmp_path, env={"ORGFACE_SLURP_GITHUB_TOKEN": "from-env"})
        assert api[0]["token"] == "from-env"

    def test_the_command_line_beats_the_file_beats_the_environment(self, runner, tmp_path, api):
        env = {"ORGFACE_SLURP_GITHUB_TOKEN": "from-env"}
        self.run(runner, tmp_path, config='[slurp.github]\ntoken = "from-file"\n', env=env)
        assert api[-1]["token"] == "from-file"
        self.run(runner, tmp_path, "--token", "from-cli", config='[slurp.github]\ntoken = "from-file"\n', env=env)
        assert api[-1]["token"] == "from-cli"

    def test_the_token_can_be_in_pass(self, runner, tmp_path, api, fake_pass):
        self.run(runner, tmp_path, config='[slurp.github]\ntoken_passname = "good"\n')
        assert api[0]["token"] == "from-pass"

    def test_a_token_given_directly_means_pass_is_not_asked(self, runner, tmp_path, api, fake_pass):
        self.run(runner, tmp_path, "--token", "direct", config='[slurp.github]\ntoken_passname = "nosuch"\n')
        assert api[0]["token"] == "direct"

    def test_a_missing_pass_entry_is_an_error_naming_it(self, runner, tmp_path, api, fake_pass):
        result = self.run(runner, tmp_path, config='[slurp.github]\ntoken_passname = "nosuch"\n')
        assert result.exit_code == 1
        assert "nosuch" in result.output
        assert api == []


class TestOrgerGithub:
    """Against real HPI and orger, with a made-up account."""

    EVENT: ClassVar[dict] = {
        "id": "1",
        "type": "ForkEvent",
        "created_at": "2026-10-01T12:00:00Z",
        "repo": {"name": "someone/project"},
        "payload": {"forkee": {"html_url": "https://github.com/me/project"}},
    }

    @pytest.fixture
    def account(self, tmp_path):
        pytest.importorskip("orger")
        pytest.importorskip("my.core")
        recent = tmp_path / "recent"
        recent.mkdir()
        (recent / "ghexport-2026-10-17.json").write_text(json.dumps({"events": [self.EVENT]}))
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "schema.json").write_text("{}")
        hpi = tmp_path / "hpi"
        (hpi / "my" / "config").mkdir(parents=True)
        (hpi / "my" / "config" / "__init__.py").write_text(
            "class github:\n"
            f"    export_path = {str(recent)!r}\n"
            f"    gdpr_dir = {str(archive)!r}\n"
        )
        return hpi

    def orgface(self, tmp_path, *args):
        env = {
            **os.environ,
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "MY_CONFIG": str(tmp_path / "hpi"),
        }
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [sys.executable, "-c", "from orgface import main; main()", *args],
            capture_output=True, text=True, cwd=tmp_path, env=env, check=False,
        )

    def test_writes_an_org_file_of_the_activity(self, tmp_path, account):
        result = self.orgface(tmp_path, "orger", "github", "--no-pandoc", "--to", "out.org")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "out.org"
        text = (tmp_path / "out.org").read_text()
        assert "someone/project: forked" in text
        assert "https://github.com/me/project" in text

    def test_can_print_instead(self, tmp_path, account):
        result = self.orgface(tmp_path, "orger", "github", "--no-pandoc", "--stdout")
        assert result.returncode == 0, result.stderr
        assert "someone/project: forked" in result.stdout
        assert not list(tmp_path.glob("*.org"))

    def test_the_output_file_is_taken_from_the_config(self, tmp_path, account):
        (tmp_path / "config" / "orgface").mkdir(parents=True)
        (tmp_path / "config" / "orgface" / "config.toml").write_text(
            f'[orger.github]\noutput = "{tmp_path / "configured.org"}"\npandoc = false\n'
        )
        result = self.orgface(tmp_path, "orger", "github")
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "configured.org").exists()
        assert not (tmp_path / "github.org").exists()


    def configure(self, tmp_path, text):
        (tmp_path / "config" / "orgface").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "orgface" / "config.toml").write_text(text)

    def test_the_timestamp_style_is_taken_from_the_config(self, tmp_path, account):
        self.configure(tmp_path, '[orger.github]\npandoc = false\ntimestamps = "active"\n')
        result = self.orgface(tmp_path, "orger", "github", "--stdout")
        assert result.returncode == 0, result.stderr
        assert "* <2026-10-01" in result.stdout  # active; inactive would be [2026-10-01

    def test_the_command_line_beats_the_config_for_both(self, tmp_path, account):
        self.configure(tmp_path, '[orger.github]\npandoc = false\ntimestamps = "active"\n')
        result = self.orgface(tmp_path, "orger", "github", "--stdout", "--timestamps", "inactive")
        assert "* [2026-10-01" in result.stdout

    def test_pandoc_false_in_the_config_means_pandoc_is_not_run(self, tmp_path, account):
        """Seen from outside: with pandoc unavailable, only a run that skips it can succeed."""
        self.configure(tmp_path, "[orger.github]\npandoc = false\n")
        # An event with a body is the thing pandoc would be asked to convert.
        fork = json.loads((tmp_path / "recent" / "ghexport-2026-10-17.json").read_text())
        fork["events"].append({
            "id": "2", "type": "IssueCommentEvent", "created_at": "2026-10-02T12:00:00Z",
            "repo": {"name": "someone/project"},
            "payload": {
                "comment": {"html_url": "https://github.com/someone/project/issues/1#c", "body": "**bold** text", "id": 5},
                "issue": {"title": "an issue"},
            },
        })
        (tmp_path / "recent" / "ghexport-2026-10-17.json").write_text(json.dumps(fork))
        result = self.orgface(tmp_path, "orger", "github", "--stdout")
        assert result.returncode == 0, result.stderr
        assert "**bold** text" in result.stdout  # converted to org it would read *bold*


class TestHpiConfigDirectory:
    def test_an_explicit_directory_is_used_if_it_has_my(self, tmp_path):
        (tmp_path / "my").mkdir()
        assert core.hpi_config_directory(tmp_path) == tmp_path

    def test_a_directory_without_my_is_ignored(self, tmp_path):
        assert core.hpi_config_directory(tmp_path) is None

    def test_my_config_names_it_otherwise(self, tmp_path, monkeypatch):
        (tmp_path / "my").mkdir()
        monkeypatch.setenv("MY_CONFIG", str(tmp_path))
        assert core.hpi_config_directory(None) == tmp_path

    def test_then_the_xdg_config_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "my" / "my").mkdir(parents=True)
        assert core.hpi_config_directory(None) == tmp_path / "my"

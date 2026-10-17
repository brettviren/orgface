# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finding sources, listing them, and running ones from outside orgface.

The external source is a real module found through a real entry point
object; only the lookup of installed distributions is pointed at it.
"""

from __future__ import annotations

import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from click.testing import CliRunner

from orgface.cli import main
from orgface.sources import BUNDLED, Source, registry

DEMO = '''
import click
from orgface.cli import pass_context
from orgface.config import parse_str, resolve_section
from orgface.sources import Source, setting

SETTINGS = (
    setting("slurp", "demo", "greeting", "hello", parse_str, "What to say."),
    setting("slurp", "demo", "secret", None, parse_str, "Hush.", secret=True),
)


@click.command("demo")
@click.option("--greeting")
@pass_context
def command(ctx, greeting):
    """Say something."""
    values = resolve_section(ctx.config, "slurp.demo", SETTINGS, {"greeting": greeting})
    click.echo(values["greeting"])


SLURP = Source(
    kind="slurp", name="demo", summary="A demonstration", command=command,
    settings=SETTINGS, requires=REQUIRES, origin="orgface-demo",
)
'''


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def external(tmp_path, monkeypatch):
    """Install a source from outside; call it with what it requires."""

    def install(requires: tuple[str, ...] = (), module: str = "orgface_demo", attr: str = "SLURP"):
        (tmp_path / f"{module}.py").write_text(
            DEMO.replace("REQUIRES", repr(requires)), encoding="utf-8"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, module, raising=False)  # one per test
        point = metadata.EntryPoint("demo", f"{module}:{attr}", "orgface.slurp")
        real = metadata.entry_points
        monkeypatch.setattr(
            metadata,
            "entry_points",
            lambda **kw: [point] if kw.get("group") == "orgface.slurp" else real(**kw),
        )

    return install


def names(kind: str) -> list[str]:
    return list(registry(kind))


class TestBundled:
    def test_github_is_both_a_slurp_and_an_orger_source(self):
        assert "github" in names("slurp")
        assert "github" in names("orger")
        assert registry("slurp")["github"].origin == BUNDLED

    def test_each_bundled_source_names_an_extra_that_exists(self):
        """Without this a source could be listed as installable and not be."""
        pyproject = Path(__file__).parents[1] / "pyproject.toml"
        extras = tomllib.loads(pyproject.read_text())["project"]["optional-dependencies"]
        for kind in ("slurp", "orger"):
            for source in registry(kind).values():
                assert (source.extra or source.name) in extras, source

    def test_a_source_lives_under_its_own_section(self):
        source = registry("orger")["github"]
        assert source.section == "orger.github"
        assert {s.section for s in source.settings} == {"orger.github"}


class TestListing:
    def test_slurp_list_shows_each_source_and_its_state(self, runner):
        result = runner.invoke(main, ["slurp", "--list"])
        assert result.exit_code == 0, result.output
        assert "github" in result.output
        assert "installed" in result.output or "missing" in result.output

    def test_list_does_not_need_a_command(self, runner):
        assert runner.invoke(main, ["orger", "--list"]).exit_code == 0

    def test_the_groups_show_help_given_nothing(self, runner):
        for group in ("slurp", "orger"):
            result = runner.invoke(main, [group])
            assert "Usage:" in result.output and "--list" in result.output

    def test_the_groups_list_their_sources_as_commands(self, runner):
        assert "github" in runner.invoke(main, ["slurp", "--help"]).output

    def test_an_unknown_source_is_an_error(self, runner):
        result = runner.invoke(main, ["slurp", "nonesuch"])
        assert result.exit_code == 2
        assert "No such command" in result.output


class TestExternal:
    def test_it_is_listed_with_where_it_came_from(self, runner, external):
        external()
        result = runner.invoke(main, ["slurp", "--list"])
        assert "demo" in result.output
        assert "A demonstration" in result.output
        assert "?" in result.output or "orgface-demo" in result.output

    def test_it_runs_and_reads_its_own_table(self, runner, external, tmp_path):
        external()
        config = tmp_path / "c.toml"
        config.write_text('[slurp.demo]\ngreeting = "from the file"\n')
        assert runner.invoke(main, ["-c", str(config), "slurp", "demo"]).output.strip() == "from the file"
        # and the command line wins
        out = runner.invoke(main, ["-c", str(config), "slurp", "demo", "--greeting", "hi"]).output
        assert out.strip() == "hi"

    def test_its_environment_variable_follows_the_convention(self, runner, external, monkeypatch):
        external()
        monkeypatch.setenv("ORGFACE_SLURP_DEMO_GREETING", "from the environment")
        assert runner.invoke(main, ["slurp", "demo"]).output.strip() == "from the environment"

    def test_a_misspelt_key_in_its_table_stops_it(self, runner, external, tmp_path):
        external()
        config = tmp_path / "c.toml"
        config.write_text('[slurp.demo]\ngreating = "x"\n')
        result = runner.invoke(main, ["-c", str(config), "slurp", "demo"])
        assert result.exit_code == 1
        assert "unknown key 'greating'" in result.output

    def test_config_show_includes_its_table_and_withholds_its_secret(self, runner, external, tmp_path):
        external()
        config = tmp_path / "c.toml"
        config.write_text('[slurp.demo]\nsecret = "sekrit"\n')
        shown = runner.invoke(main, ["-c", str(config), "config", "show"]).output
        assert "[slurp.demo]" in shown
        assert 'greeting = "hello"' in shown
        assert "sekrit" not in shown
        assert "sekrit" in runner.invoke(main, ["-c", str(config), "config", "show", "--secrets"]).output

    def test_one_that_will_not_load_does_not_break_the_others(self, runner, external, caplog):
        external(attr="NOSUCH")
        assert "github" in names("slurp")
        assert "demo" not in names("slurp")
        assert "NOSUCH" in caplog.text or "demo" in caplog.text

    def test_a_bundled_source_cannot_be_shadowed(self, runner, external, tmp_path, monkeypatch):
        external()
        (tmp_path / "orgface_demo.py").write_text(
            (tmp_path / "orgface_demo.py").read_text().replace('name="demo"', 'name="github"')
        )
        # the entry point is called demo, the source github: refused
        assert registry("slurp")["github"].origin == BUNDLED


class TestMisplacedSetting:
    """A source has a table for each half; a key in the other half's is easy to do."""

    def test_a_setting_in_the_other_halfs_table_is_refused_by_both_halves(self, runner, tmp_path):
        config = tmp_path / "c.toml"
        config.write_text("[slurp.github]\npandoc = false\n")
        for argv in (["orger", "github", "--stdout"], ["slurp", "github", "archive", "nonesuch"]):
            result = runner.invoke(main, ["-c", str(config), *argv])
            assert result.exit_code == 1, argv
            assert "[slurp.github]: unknown key 'pandoc'" in result.output

    def test_it_is_not_refused_in_its_own_table(self, runner, tmp_path):
        config = tmp_path / "c.toml"
        config.write_text("[orger.github]\npandoc = false\n")
        result = runner.invoke(main, ["-c", str(config), "orger", "--list"])
        assert result.exit_code == 0

    def test_a_table_of_an_unknown_source_is_left_alone(self, runner, tmp_path):
        config = tmp_path / "c.toml"
        config.write_text("[slurp.absent]\nanything = 1\n")
        assert runner.invoke(main, ["-c", str(config), "slurp", "github", "archive", "-h"]).exit_code == 0


class TestMissingDependencies:
    def test_it_is_listed_as_missing_with_how_to_get_it(self, runner, external):
        external(requires=("no_such_module_anywhere",))
        out = runner.invoke(main, ["slurp", "--list"]).output
        assert "missing" in out
        assert "install" in out

    def test_running_it_says_what_to_do_and_fails(self, runner, external):
        external(requires=("no_such_module_anywhere",))
        result = runner.invoke(main, ["slurp", "demo", "--whatever", "args"])
        assert result.exit_code == 1
        assert "no_such_module_anywhere" in result.output
        assert "not installed" in result.output

    def test_help_for_it_still_works(self, runner, external):
        external(requires=("no_such_module_anywhere",))
        result = runner.invoke(main, ["slurp", "demo", "--help"])
        assert result.exit_code == 0
        assert "Not installed" in result.output

    def test_source_reports_what_is_missing(self):
        source = Source(
            kind="slurp", name="x", summary="", command=registry("slurp")["github"].command,
            requires=("os", "no_such_module_anywhere"),
        )
        assert source.missing() == ("no_such_module_anywhere",)
        assert not source.installed
        assert "'.[x]'" in source.install_hint()

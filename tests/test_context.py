# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The context ``org note`` records, and the template it becomes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from orgface import context
from orgface import templates as tmpl

WHEN = datetime(2026, 8, 4, 13, 45, 2)


def make(directory: Path, **kwargs) -> context.RunContext:
    """A context with everything pinned down, as the tests need it."""
    kwargs.setdefault("now", WHEN)
    kwargs.setdefault("host", "somehost")
    kwargs.setdefault("user", "someone")
    kwargs.setdefault("where_home", directory)
    return context.gather(directory=directory, **kwargs)


class TestOrgTag:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("orgface", "orgface"),
            ("plain", "plain"),
            ("Mixed_Case9", "Mixed_Case9"),
            ("dots.and spaces", "dots_and_spaces"),
            ("-leading-and-trailing-", "leading_and_trailing"),
            ("many---separators", "many_separators"),
            ("keeps@and#", "keeps@and#"),
            ("100%", "100"),  # % is a tag character to org but not to capture
            ("...", ""),
            ("", ""),
        ],
    )
    def test_sanitises(self, name, expected):
        assert context.org_tag(name) == expected


class TestGitRoot:
    def test_finds_the_directory_holding_dot_git(self, tmp_path):
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        deep = tmp_path / "repo" / "src" / "deep"
        deep.mkdir(parents=True)
        assert context.git_root(deep) == tmp_path / "repo"

    def test_the_root_itself_counts(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert context.git_root(tmp_path) == tmp_path

    def test_a_dot_git_file_counts_too(self, tmp_path):
        """git leaves a file, not a directory, in a worktree checkout."""
        (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        assert context.git_root(tmp_path) == tmp_path

    def test_none_outside_a_working_tree(self, tmp_path):
        assert context.git_root(tmp_path) is None


class TestRunContext:
    def test_gathers_what_it_was_given(self, tmp_path):
        run = make(tmp_path)
        assert run.directory == tmp_path.resolve()
        assert run.host == "somehost"
        assert run.user == "someone"
        assert run.when == WHEN

    def test_seconds_but_no_finer(self, tmp_path):
        run = make(tmp_path, now=datetime(2026, 8, 4, 13, 45, 2, 987654))
        assert run.when.microsecond == 0
        assert run.timestamp == "[2026-08-04 Tue 13:45:02]"

    def test_timestamp_is_inactive(self, tmp_path):
        """An active timestamp would put every note in the agenda."""
        assert make(tmp_path).timestamp.startswith("[")

    def test_properties(self, tmp_path):
        assert make(tmp_path).properties() == (
            ("DIRECTORY", str(tmp_path.resolve())),
            ("HOST", "somehost"),
            ("USER", "someone"),
        )

    def test_home_is_tagged(self, tmp_path):
        assert make(tmp_path, where_home=tmp_path).tags == ("home",)

    def test_elsewhere_is_not_tagged(self, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        assert make(other, where_home=tmp_path).tags == ()

    def test_a_symlinked_home_still_matches(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert make(link, where_home=real).tags == ("home",)

    def test_git_working_tree_is_tagged(self, tmp_path):
        repo = tmp_path / "some-repo"
        (repo / ".git").mkdir(parents=True)
        work = repo / "src"
        work.mkdir()
        assert make(work, where_home=tmp_path).tags == ("some_repo",)

    def test_both_tags_at_once(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert make(tmp_path, where_home=tmp_path).tags == ("home", tmp_path.name)

    def test_an_unusable_repository_name_yields_no_tag(self, tmp_path):
        repo = tmp_path / "..."
        (repo / ".git").mkdir(parents=True)
        assert make(repo, where_home=tmp_path).tags == ()

    def test_a_missing_home_is_survivable(self, tmp_path):
        assert make(tmp_path, where_home=None).tags == ()


class TestNoteTemplate:
    def test_shape(self, tmp_path):
        body = context.note_template(make(tmp_path)).body
        assert body.splitlines() == [
            f"* {context.TITLE_PROMPT} :home:",
            ":PROPERTIES:",
            f":DIRECTORY: {tmp_path.resolve()}",
            ":HOST: somehost",
            ":USER: someone",
            ":END:",
            "[2026-08-04 Tue 13:45:02]",
            "",
            "%?",
        ]

    def test_no_tags_means_no_suffix(self, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        template = context.note_template(make(other, where_home=tmp_path))
        assert template.body.startswith(f"* {context.TITLE_PROMPT}\n")

    def test_fills_like_any_other_template(self, tmp_path):
        template = context.note_template(make(tmp_path))
        filled = tmpl.fill(template, "A title", "A body.")
        assert "* A title :home:" in filled.text
        assert filled.text.endswith("%?A body.")
        assert filled.warnings == ()

    def test_a_percent_in_the_directory_is_escaped(self, tmp_path):
        """A path org-capture would expand has to survive two fillings."""
        directory = tmp_path / "100%t of it"
        directory.mkdir()
        body = context.note_template(make(directory)).body
        assert ":DIRECTORY: " + str(tmp_path) + "/100" in body
        assert "\\" * tmpl.backslashes(0) + "%t" in body

    def test_a_roam_placeholder_in_the_directory_is_refused(self, tmp_path):
        directory = tmp_path / "${title}"
        directory.mkdir()
        with pytest.raises(tmpl.TemplateError, match="directory"):
            context.note_template(make(directory))

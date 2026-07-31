# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Template selection and org-capture-safe filling."""

from __future__ import annotations

import pytest

from org_cli_helper.templates import (
    ROUNDS,
    Template,
    TemplateError,
    backslashes,
    check_expandable,
    escape,
    fill,
    join_body,
    parse_templates,
    select,
    selectable,
)

DAILY = Template("d", "default", "entry", "* %^{Title}\n%T\n%?")
STOCK = Template("s", "stock", "entry", "* %?")
DIRECTORY = Template("D", "directory", "entry", "* %^{Title}\n%?")


def test_parse_templates_reads_daemon_payload():
    parsed = parse_templates(
        [
            {
                "key": "d",
                "description": "default",
                "type": "entry",
                "body": "* %?",
                "body_kind": "string",
                "group": False,
            },
            {"key": "b", "description": "Bookmarks", "group": True},
        ]
    )
    assert [t.key for t in parsed] == ["d", "b"]
    assert parsed[1].group is True
    assert [t.key for t in selectable(parsed)] == ["d"]


class TestSelect:
    def test_default_is_first(self):
        assert select([DAILY, DIRECTORY], None) is DAILY

    def test_by_key(self):
        assert select([DAILY, DIRECTORY], "D") is DIRECTORY

    def test_by_index(self):
        assert select([DAILY, DIRECTORY], "1") is DIRECTORY

    def test_by_description(self):
        assert select([DAILY, DIRECTORY], "directory") is DIRECTORY

    def test_description_is_case_insensitive(self):
        assert select([DAILY, DIRECTORY], "DIRECTORY") is DIRECTORY

    def test_description_prefix(self):
        assert select([DAILY, DIRECTORY], "dir") is DIRECTORY

    def test_key_beats_index(self):
        """A template keyed "1" wins over the template at index 1."""
        numeric = Template("1", "numeric", "entry", "* %?")
        assert select([DAILY, DIRECTORY, numeric], "1") is numeric

    def test_groups_are_not_selectable(self):
        group = Template("b", "Bookmarks", group=True)
        assert select([group, DAILY], None) is DAILY
        with pytest.raises(TemplateError):
            select([group, DAILY], "b")

    def test_index_out_of_range(self):
        with pytest.raises(TemplateError, match="out of range"):
            select([DAILY], "7")

    def test_unknown_spec_lists_available(self):
        with pytest.raises(TemplateError, match="available templates"):
            select([DAILY], "nope")

    def test_ambiguous_prefix(self):
        a = Template("a", "daily plain", "entry", "* %?")
        b = Template("b", "daily fancy", "entry", "* %?")
        with pytest.raises(TemplateError, match="ambiguous"):
            select([a, b], "daily")

    def test_no_templates(self):
        with pytest.raises(TemplateError, match="no usable"):
            select([], None)


class TestBackslashes:
    """The escape arithmetic, verified against Emacs in test_emacs_live.py."""

    def test_one_round(self):
        """org-capture deletes (n+1)/2 of n backslashes, so 1 leaves 0."""
        assert backslashes(0, rounds=1) == 1
        assert backslashes(1, rounds=1) == 3

    def test_two_rounds(self):
        """org-roam fills the body, then org-capture fills it again."""
        assert backslashes(0, rounds=2) == 3
        assert backslashes(1, rounds=2) == 7

    def test_default_is_two_rounds(self):
        assert ROUNDS == 2
        assert backslashes(0) == backslashes(0, rounds=2)

    @pytest.mark.parametrize("rounds", [1, 2, 3])
    @pytest.mark.parametrize("literal", [0, 1, 2])
    def test_simulating_emacs_leaves_the_literal_run(self, literal, rounds):
        """Model org-capture-escaped-% and check the round trip."""
        count = backslashes(literal, rounds)
        for _ in range(rounds):
            assert count % 2 == 1, "must stay escaped at every pass"
            count -= (count + 1) // 2
        assert count == literal


class TestEscape:
    """Org escapes a placeholder with a preceding backslash, and only there."""

    @pytest.mark.parametrize(
        "text",
        [
            "%t",
            "%T",
            "%<%Y>",
            "%:link",
            "%(shell-command)",
            "%^{prompt}",
            "%[/etc/passwd]",
            r"%\1",
        ],
    )
    def test_placeholders_are_escaped(self, text):
        assert escape(text) == "\\\\\\" + text

    @pytest.mark.parametrize(
        "text",
        ["100% done", "50%", "a % b", "%%", "%?", "% ", "%z"],
    )
    def test_non_placeholders_are_left_alone(self, text):
        """A stray backslash would survive into the note, so add none."""
        assert escape(text) == text

    def test_existing_backslashes_are_grown(self):
        """One literal backslash before an escaped % needs 4*1+3 of them."""
        assert escape(r"\%t") == "\\" * 7 + "%t"

    def test_backslash_not_before_placeholder_untouched(self):
        assert escape(r"C:\path") == r"C:\path"


class TestCheckExpandable:
    """org-roam's ${...} has no escape, so it must be refused outright."""

    @pytest.mark.parametrize(
        "text", ["set ${CMAKE_PREFIX_PATH}", "${shell}", "a ${x=1} b"]
    )
    def test_refused(self, text):
        with pytest.raises(TemplateError, match="no escape available"):
            check_expandable(text)

    @pytest.mark.parametrize("text", ["$HOME", "${unclosed", "cost $5", "a } b"])
    def test_allowed(self, text):
        check_expandable(text)

    def test_names_the_offending_field(self):
        with pytest.raises(TemplateError, match="^body contains"):
            check_expandable("${x}", "body")

    def test_fill_refuses_in_title_and_body(self):
        with pytest.raises(TemplateError, match="^title contains"):
            fill(DAILY, "${x}", "")
        with pytest.raises(TemplateError, match="^body contains"):
            fill(DAILY, "ok", "${x}")


class TestFill:
    def test_prompt_takes_title_and_marker_precedes_body(self):
        filled = fill(DAILY, "Fixed the thing", "Took all day.")
        assert filled.text == "* Fixed the thing\n%T\n%?Took all day."
        assert filled.warnings == ()

    def test_template_without_prompt_leads_with_title(self):
        filled = fill(STOCK, "Fixed the thing", "Took all day.")
        assert filled.text == "* %?Fixed the thing\n\nTook all day."

    def test_title_only(self):
        assert fill(DAILY, "Just this", "").text == "* Just this\n%T\n%?"

    def test_user_percent_is_escaped(self):
        filled = fill(DAILY, "100% done", "Used %t of the budget")
        # "% d" is not a placeholder; "%t" is, and needs two rounds of escape.
        assert filled.text == (
            "* 100% done\n%T\n%?Used " + "\\" * 3 + "%t of the budget"
        )

    def test_extra_prompts_are_emptied_with_a_warning(self):
        template = Template("x", "extra", "entry", "* %^{Title}\n%^{Mood}\n%?")
        filled = fill(template, "Title", "Body")
        assert filled.text == "* Title\n\n%?Body"
        assert filled.warnings == ("left template prompt 'Mood' empty",)

    @pytest.mark.parametrize("escape_form", ["%^t", "%^{When}u", "%^{Prop}p"])
    def test_uninhabitable_interactive_escapes_are_refused(self, escape_form):
        template = Template("i", "interactive", "entry", f"* %^{{Title}}\n{escape_form}\n%?")
        with pytest.raises(TemplateError, match="interactive"):
            fill(template, "Title", "Body")

    def test_missing_marker_is_appended(self):
        template = Template("n", "no marker", "entry", "* Fixed")
        filled = fill(template, "Title", "Body")
        assert filled.text == "* Fixed\n%?Title\n\nBody"

    def test_nil_body_uses_org_type_default(self):
        template = Template("z", "empty", "entry", None, body_kind="nil")
        # org-capture-set-plist substitutes "* %?\n  %a" for entry.
        assert fill(template, "Title", "").text == "* %?Title\n  %a"

    def test_percent_question_in_title_warns(self):
        """Org's cursor search honours no escaping, so say so rather than lie."""
        filled = fill(DAILY, "what %? means", "Body")
        assert any("cursor marker" in w for w in filled.warnings)

    def test_percent_question_in_body_survives(self):
        """Our marker comes first, so Org consumes ours and the body is safe."""
        filled = fill(DAILY, "Title", "what %? means")
        assert filled.text.endswith("%?what %? means")
        assert filled.warnings == ()


class TestJoinBody:
    def test_paragraphs_are_blank_line_separated(self):
        assert join_body(["one", "two"]) == "one\n\ntwo"

    def test_empty_messages_are_dropped(self):
        assert join_body(["one", "  ", "two"]) == "one\n\ntwo"

    def test_no_messages(self):
        assert join_body([]) == ""

# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Writing the canonical model back out, and the round trip through it.

The round-trip tests are the point: whatever the renderer emits has to
parse back into the same document, or the org-file store loses content
every time it writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from org_store.adapters.orgparser import parse, parse_file
from org_store.extract import content_hash
from org_store.model import OrgDocument, walk
from org_store.render import OrgRenderer, render_document, render_node

DATA = Path(__file__).parent / "data"

#: Every org fixture in the corpus, round-tripped by name.
CORPUS = sorted(path.name for path in DATA.glob("*.org"))


def roundtrip(text: str) -> tuple[OrgDocument, OrgDocument]:
    """Parse TEXT, render it, and parse the result."""
    first = parse(text)
    return first, parse(render_document(first))


# -- round trips ------------------------------------------------------------


@pytest.mark.parametrize("name", CORPUS)
def test_corpus_round_trips_without_loss(name: str) -> None:
    """Parse, render, re-parse: the document must be unchanged.

    Compared by content hash, which ignores refs but nothing else -- so
    any content the renderer drops or the parser cannot read back shows
    up here rather than being quietly accepted.
    """
    first, second = roundtrip((DATA / name).read_text())
    assert content_hash(first) == content_hash(second), name


@pytest.mark.parametrize("name", CORPUS)
def test_rendering_is_idempotent(name: str) -> None:
    """Saving a file twice must not keep changing it."""
    first = parse_file(DATA / name)
    once = render_document(first)
    assert render_document(parse(once)) == once, name


@pytest.mark.parametrize(
    "source",
    [
        "* Heading\n",
        "* TODO [#A] Heading :tag:\n",
        "Just a paragraph.\n",
        "#+title: A title\n",
        "- one\n- two\n",
        "| a | b |\n|---+---|\n| 1 | 2 |\n",
        "#+begin_src sh\necho hi\n#+end_src\n",
        "#+begin_quote\nQuoted.\n#+end_quote\n",
        "#+begin_example\nliteral\n#+end_example\n",
        "Text with *bold* and /italic/ and ~code~.\n",
        "A [[https://example.org][link]] here.\n",
        "Maths $a=b$ inline.\n",
        "\\begin{equation}\nx = 1\n\\end{equation}\n",
        "A reference[fn:1] here.\n\n[fn:1] The note.\n",
        "* Parent\n:PROPERTIES:\n:ID: abc\n:END:\n\n** Child\n",
        "* Scheduled\nSCHEDULED: <2026-08-24 Mon>\n",
        ": fixed width\n",
        "1. one\n2. two\n",
        "3. three\n4. four\n",
        "See https://example.org here.\n",
        "See <mailto:someone@example.org> here.\n",
        "#+name: snip\n#+begin_src sh\nls\n#+end_src\n",
        "#+caption: A cap\n#+begin_src sh\nls\n#+end_src\n",
        "#+title: T\n:PROPERTIES:\n:ID: abc\n:END:\n\nBody.\n",
        "- Item :: first line\n  second line\n  third line\n",
        "- A plain item running on\n  to a second line\n  and a third\n",
        "#+begin_src org\n,* A heading\n#+end_src\n",
        "#+begin_quote\nQuoted.\n\nTwice.\n#+end_quote\n",
    ],
)
def test_constructs_round_trip(source: str) -> None:
    first, second = roundtrip(source)
    assert content_hash(first) == content_hash(second)


def test_round_trip_keeps_the_node_census() -> None:
    """Not just equal hashes -- the same nodes, of the same types."""
    first, second = roundtrip((DATA / "fixture.org").read_text())
    assert [n.type for n in walk(first)] == [n.type for n in walk(second)]


# -- what the rendering looks like ------------------------------------------


def test_headline_line() -> None:
    doc = parse("* TODO [#A] Something :work:\n")
    text = render_document(doc)
    assert text.splitlines()[0] == "* TODO [#A] Something :work:"


def test_keywords_are_not_spaced_apart() -> None:
    """A blank line between two #+keyword: lines would end the run."""
    text = render_document(parse("#+title: T\n#+author: A\n"))
    assert "#+title: T\n#+author: A\n" in text


def test_file_keywords_are_written_once() -> None:
    """They live in the first section; emitting them again would duplicate."""
    text = render_document(parse("#+title: Only once\n\nBody.\n"))
    assert text.count("#+title:") == 1


def test_property_drawer() -> None:
    text = render_document(parse("* H\n:PROPERTIES:\n:ID: xyz\n:END:\n"))
    assert ":PROPERTIES:\n:ID: xyz\n:END:" in text


def test_block_content_is_written_with_orgs_escape() -> None:
    """The model holds what the block says; org spells a star line ``,*``."""
    text = render_document(parse("#+begin_src org\n* A heading\n#+end_src\n"))
    assert text == "#+begin_src org\n,* A heading\n#+end_src\n"


def test_an_affiliated_keyword_is_not_spaced_off_its_block() -> None:
    """A blank line after ``#+name:`` would leave it naming nothing."""
    text = render_document(parse("#+name: snip\n#+begin_src sh\nls\n#+end_src\n"))
    assert text.startswith("#+name: snip\n#+begin_src sh\n")


def test_ordered_lists_are_written_with_their_numbers() -> None:
    text = render_document(parse("1. one\n2. two\n"))
    assert text == "1. one\n2. two\n"


def test_a_special_block_is_written_with_its_own_name() -> None:
    text = render_document(parse("#+begin_note\nSomething.\n#+end_note\n"))
    assert text == "#+begin_note\nSomething.\n#+end_note\n"


def test_a_special_block_without_a_name_still_renders() -> None:
    """A document need not have come from the reader; it may lack the name."""
    doc = OrgDocument.model_validate(
        {
            "contents": [
                {
                    "$$data_type": "org-node",
                    "type": "special-block",
                    "properties": {},
                    "contents": [
                        {
                            "$$data_type": "org-node",
                            "type": "paragraph",
                            "properties": {},
                            "contents": ["Something."],
                        }
                    ],
                }
            ]
        }
    )
    assert render_document(doc) == "#+begin_special\nSomething.\n#+end_special\n"


def test_indentation_is_kept_where_it_was_written() -> None:
    """Rewriting a file should not shift its indented prose to the margin."""
    text = render_document(parse("* H\n\n  indented paragraph\n"))
    assert text == "* H\n\n  indented paragraph\n"


@pytest.mark.parametrize(
    "source",
    [
        "- Item :: first line\n  second line\n  third line\n",
        "- plain item\n  second line\n  third line\n  fourth line\n",
        "#+begin_src org\n,* A heading\n#+end_src\n",
        "#+name: snip\n#+begin_src sh\nls\n#+end_src\n",
        "#+title: T\n:PROPERTIES:\n:ID: abc\n:END:\n\nBody.\n",
        "#+begin_note\nSomething.\n#+end_note\n",
        "#+begin_note :class warn\nSomething.\n#+end_note\n",
        "* H\n\n  indented paragraph\n  second line\n",
        "* H\n\n  - one\n  - two\n",
    ],
)
def test_already_canonical_text_is_written_back_unchanged(source: str) -> None:
    """A file written by the renderer must survive being read and written again.

    Not merely idempotent from the second write on: text in the form the
    renderer produces is a fixed point on the first.  A list item running
    to three lines used to gain two spaces of indent on one pass and lose
    them on the next, so no version of the file was stable.
    """
    assert render_document(parse(source)) == source


def test_planning_line_follows_the_heading() -> None:
    text = render_document(parse("* H\nSCHEDULED: <2026-08-24 Mon>\n"))
    lines = text.splitlines()
    assert lines[0] == "* H"
    assert lines[1] == "SCHEDULED: <2026-08-24 Mon>"


def test_blocks_are_separated_by_one_blank_line() -> None:
    text = render_document(parse("One.\n\nTwo.\n"))
    assert text == "One.\n\nTwo.\n"


def test_list_items_are_not_separated() -> None:
    """A blank line between items would split the list in two."""
    text = render_document(parse("- one\n- two\n"))
    assert text == "- one\n- two\n"


def test_maths_is_written_verbatim() -> None:
    text = render_document(parse("Before $E = mc^2$ after.\n"))
    assert "$E = mc^2$" in text


def test_inline_node_has_no_trailing_newline() -> None:
    doc = parse("Some *bold* text.\n")
    bold = next(n for n in walk(doc) if n.type == "bold")
    assert render_node(bold) == "*bold*"


def test_block_node_ends_with_one_newline() -> None:
    doc = parse("#+begin_src sh\necho hi\n#+end_src\n")
    block = next(n for n in walk(doc) if n.type == "src-block")
    rendered = render_node(block)
    assert rendered.endswith("#+end_src\n")
    assert not rendered.endswith("\n\n")


def test_plain_text_passes_through() -> None:
    assert render_node("just text") == "just text"


# -- templates --------------------------------------------------------------


def test_unknown_type_uses_the_fallback() -> None:
    """An unrecognised node prints its text rather than vanishing."""
    doc = OrgDocument.model_validate(
        {
            "contents": [
                {
                    "$$data_type": "org-node",
                    "type": "something-new",
                    "properties": {"value": "kept"},
                }
            ]
        }
    )
    assert "kept" in render_document(doc)


def test_every_typed_node_has_a_template_or_falls_back() -> None:
    """Rendering must never raise for a type the model knows about."""
    from org_store.model import node_types

    renderer = OrgRenderer()
    for kind in node_types():
        node = OrgDocument.model_validate(
            {"contents": [{"$$data_type": "org-node", "type": kind}]}
        ).contents[0]
        renderer.render(node)  # must not raise


def test_templates_are_a_directory_of_files() -> None:
    from org_store.render.renderer import TEMPLATES

    names = {path.name for path in TEMPLATES.glob("*.org.j2")}
    assert "headline.org.j2" in names
    assert "fallback.org.j2" in names
    assert len(names) > 20


def test_a_custom_template_directory_is_honoured(tmp_path) -> None:
    (tmp_path / "paragraph.org.j2").write_text("PARA: {{ content }}\n")
    (tmp_path / "fallback.org.j2").write_text("{{ content }}{{ value }}")
    (tmp_path / "section.org.j2").write_text("{{ content }}")
    rendered = OrgRenderer(tmp_path).document(parse("hello\n"))
    assert "PARA: hello" in rendered


def test_json_fixture_renders_too() -> None:
    """A document from Emacs renders with the same templates."""
    raw = json.loads((DATA / "fixture.ox-json.json").read_text())
    text = render_document(OrgDocument.model_validate(raw))
    assert "* TODO [#A] A headline with tags" in text
    assert "$E = mc^2$" in text

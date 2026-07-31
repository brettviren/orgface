"""Selecting and filling org-roam dailies capture templates.

This module is pure: it operates on template descriptions already fetched
from the daemon and produces a template string ready to be handed back.
Nothing here talks to Emacs.

Filling a template means splicing user text into a string that Emacs will
subsequently expand, so every ``%`` in that user text has to be protected
from `org-capture-fill-template`.  Org escapes a placeholder with a
*preceding backslash*, counted by `org-capture-escaped-%`: for a run of n
backslashes it deletes (n+1)/2 of them and treats the placeholder as
escaped when n is odd.

An org-roam capture body is expanded **twice**.
`org-roam-capture--prepare-buffer` runs during
`org-capture-set-target-location` and puts the result of
`org-roam-capture--fill-template' back into `:template', after which
org-capture fills that already-filled text again.  One round of escaping is
therefore consumed by the first pass and the placeholder would be live for
the second, so text must be escaped for :data:`ROUNDS` passes -- see
:func:`backslashes` for the arithmetic.

Crucially the escape only applies where Org actually recognises a
placeholder; a ``%`` followed by anything else is left alone, and adding a
backslash there would survive into the captured text.  Hence the precise
placeholder pattern below rather than a blanket escape.

``${...}`` has no escape at all: `org-roam-format-template` matches it with
a plain regexp and either calls a same-named function or prompts for a
value, neither of which is survivable non-interactively.  Such text is
refused rather than silently mangled or executed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# Placeholders recognised by org-capture-fill-template, in the same terms
# as the regexps in org-capture.el:
#   %[FILE]          file inclusion
#   %:keyword        link plist property
#   %<...>           format-time-string
#   %a %A %c %f ...  single letter expansions
#   %(               embedded elisp
#   %^ ...           interactive prompts
#   %\N              nth prompt answer
_PLACEHOLDER = (
    r"%(?:\[.+\]|:[-A-Za-z]+|<[^>\n]+>|[aAcfFikKlLntTuUx]|\(|\^|\\[1-9][0-9]*)"
)

_ESCAPE_RE = re.compile(r"(\\*)(" + _PLACEHOLDER + r")")

# org-roam's ${var} substitution, which offers no escape mechanism.
_ROAM_VAR_RE = re.compile(r"\$\{[^}]+\}")

#: How many times an org-roam capture body gets expanded.  See the module
#: docstring: org-roam fills the template and hands the result back to
#: org-capture, which fills it again.
ROUNDS = 2

# %^{PROMPT} and friends, used when filling and when detecting prompts we
# cannot answer non-interactively.
_PROMPT_RE = re.compile(r"%\^(?:\{([^}]*)\})?([CgGLptTuU])?")

# Default template bodies Org substitutes for an empty/missing template,
# from `org-capture-set-plist'.
_TYPE_DEFAULTS = {
    "item": "- %?",
    "checkitem": "- [ ] %?",
    "table-line": "| %? |",
    "entry": "* %?\n  %a",
    "plain": "%?",
}


class TemplateError(Exception):
    """A template could not be selected or filled."""


@dataclass(frozen=True)
class Template:
    """One entry of ``org-roam-dailies-capture-templates``."""

    key: str
    description: str
    type: str = "entry"
    body: str | None = None
    body_kind: str = "string"  # string | file | function | nil
    group: bool = False  # a multi-key prefix group, not selectable

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Template:
        return cls(
            key=data["key"],
            description=data.get("description") or "",
            type=data.get("type") or "entry",
            body=data.get("body"),
            body_kind=data.get("body_kind") or "nil",
            group=bool(data.get("group")),
        )

    def effective_body(self) -> str:
        """The template string Org would use, filling in type defaults."""
        if self.body_kind != "string" or self.body is None or not self.body.strip():
            return _TYPE_DEFAULTS.get(self.type, "%?")
        return self.body

    def label(self) -> str:
        return f"{self.key}\t{self.description}"


def parse_templates(payload: Iterable[dict[str, Any]]) -> list[Template]:
    return [Template.from_json(item) for item in payload]


def selectable(templates: Sequence[Template]) -> list[Template]:
    """Templates a capture can actually use (prefix groups excluded)."""
    return [t for t in templates if not t.group]


def select(templates: Sequence[Template], spec: str | None) -> Template:
    """Resolve a ``-t/--template`` spec to a single template.

    ``spec`` may be a template key, a 0-based index into the selectable
    templates, or a description.  Precedence is key, then index, then
    description, so a template whose key is ``"1"`` still wins over the
    template at index 1.  A None spec selects the first template.
    """
    usable = selectable(templates)
    if not usable:
        raise TemplateError("no usable dailies capture templates are defined")

    if spec is None:
        return usable[0]

    for template in usable:
        if template.key == spec:
            return template

    if re.fullmatch(r"-?\d+", spec):
        index = int(spec)
        if -len(usable) <= index < len(usable):
            return usable[index]
        raise TemplateError(
            f"template index {index} out of range "
            f"(have {len(usable)})\n{available(templates)}"
        )

    exact = [t for t in usable if t.description == spec]
    if len(exact) == 1:
        return exact[0]

    folded = spec.casefold()
    loose = [t for t in usable if t.description.casefold() == folded]
    if len(loose) == 1:
        return loose[0]

    partial = [t for t in usable if t.description.casefold().startswith(folded)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(repr(t.description) for t in partial)
        raise TemplateError(f"template {spec!r} is ambiguous: matches {names}")

    raise TemplateError(f"no such template: {spec!r}\n{available(templates)}")


def available(templates: Sequence[Template]) -> str:
    """A human readable listing for error messages and ``--list``."""
    usable = selectable(templates)
    if not usable:
        return "no templates available"
    width = max(len(t.key) for t in usable)
    lines = ["available templates:"]
    for index, template in enumerate(usable):
        lines.append(
            f"  [{index}] {template.key:<{width}}  {template.description}"
        )
    return "\n".join(lines)


def backslashes(literal: int, rounds: int = ROUNDS) -> int:
    """Backslashes needed so ROUNDS expansions leave LITERAL of them.

    One expansion turns a run of n backslashes (n odd, so the placeholder
    counts as escaped) into (n-1)/2, which inverts to n = 2m+1.  Applying
    that inverse ROUNDS times gives the closed form below, and every
    intermediate count stays odd so the placeholder survives each pass.
    """
    return (2**rounds) * literal + (2**rounds - 1)


def check_expandable(text: str, where: str = "message") -> None:
    """Refuse text org-roam would expand with no way to escape it."""
    found = _ROAM_VAR_RE.search(text)
    if found:
        raise TemplateError(
            f"{where} contains {found.group(0)!r}, which org-roam expands "
            f"with no escape available: it would call a function named "
            f"{found.group(0)[2:-1]!r} or prompt for a value. "
            f"Rewrite it, or use 'commit' without -m to capture interactively."
        )


def escape(text: str, rounds: int = ROUNDS) -> str:
    """Protect literal user text from org-capture placeholder expansion."""

    def repl(match: re.Match[str]) -> str:
        run, placeholder = match.group(1), match.group(2)
        return "\\" * backslashes(len(run), rounds) + placeholder

    return _ESCAPE_RE.sub(repl, text)


def join_body(messages: Sequence[str]) -> str:
    """Join ``-m`` body messages the way git joins commit paragraphs."""
    return "\n\n".join(m.strip("\n") for m in messages if m.strip())


@dataclass(frozen=True)
class Filled:
    """A template body ready for a non-interactive capture."""

    text: str
    warnings: tuple[str, ...] = ()


def fill(template: Template, title: str, body: str) -> Filled:
    """Splice ``title`` and ``body`` into ``template``'s body string.

    The first ``%^{...}`` prompt receives the title; any further ``%^{...}``
    prompts are filled empty (they cannot be answered without a terminal)
    and reported as warnings.  Other interactive escapes such as ``%^t``
    have no sensible non-interactive answer and are refused.

    The body replaces ``%?``.  Org locates the cursor by a plain
    ``search-forward`` for ``%?`` that honours no escaping, so the marker is
    re-emitted immediately before the body: Org consumes that one and a
    literal ``%?`` inside the user's text survives.
    """
    check_expandable(title, "title")
    check_expandable(body, "body")

    source = template.effective_body()
    warnings: list[str] = []

    # Spans are all computed against the untouched template text and applied
    # back to front, so one substitution cannot disturb another's offsets.
    spans: list[tuple[int, int, str]] = []

    title_used = False
    for match in _PROMPT_RE.finditer(source):
        braces, key = match.group(1), match.group(2)
        if key:
            # %^t, %^{prompt}p and friends need a person at a keyboard.
            shown = f"%^{{...}}{key}" if braces is not None else f"%^{key}"
            raise TemplateError(
                f"template {template.key!r} uses the interactive escape "
                f"{shown!r}, which has no non-interactive answer; "
                f"run 'commit' without -m to capture interactively"
            )
        if not title_used:
            spans.append((match.start(), match.end(), escape(title)))
            title_used = True
        else:
            name = (braces or "").split("|")[0] or "prompt"
            warnings.append(f"left template prompt {name!r} empty")
            spans.append((match.start(), match.end(), ""))

    payload = escape(body) if body else ""
    if not title_used:
        # No prompt in the template: the title leads the body instead.
        payload = "\n\n".join(part for part in (escape(title), payload) if part)

    marker = "%?"
    at = source.find(marker)
    if at >= 0:
        spans.append((at, at + len(marker), marker + payload))
    else:
        tail = "" if source.endswith("\n") or not source else "\n"
        at = len(source) + len(tail)
        source = source + tail
        spans.append((at, at, marker + payload))

    text = source
    for start, end, value in sorted(spans, reverse=True):
        text = text[:start] + value + text[end:]

    # Org finds the cursor with a plain search for "%?" that honours no
    # escaping, so anything ahead of our marker that looks like one wins.
    marker_at = text.find(marker + payload)
    if text.find(marker) != marker_at:
        warnings.append(
            "text before the capture point contains '%?', which "
            "org-capture will consume as its cursor marker"
        )

    return Filled(text=text, warnings=tuple(warnings))

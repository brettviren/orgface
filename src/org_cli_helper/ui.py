# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Rendering and paging.

Presentation only: everything here takes already-computed values and turns
them into text.  No file reading, no Emacs, no policy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import click

from .dailies import Entry

# git's own default; -R keeps ANSI colour, -F/-X avoid clearing short output.
LESS_DEFAULTS = "FRX"


def style(text: str, colour: str | None, enabled: bool, bold: bool = False) -> str:
    """Colourise TEXT, emitting no codes at all when they are not wanted.

    ``bold=False`` is passed as None so Click leaves out the redundant
    "normal intensity" reset.
    """
    if not enabled:
        return text
    if colour is None and not bold:
        return text
    return click.style(text, fg=colour, bold=bold or None)


def indent(text: str, prefix: str = "    ") -> str:
    """Indent every non-blank line, the way git log indents a message."""
    return "\n".join(
        prefix + line if line.strip() else "" for line in text.split("\n")
    )


def format_tags(tags: Sequence[str], separate: bool = True) -> str:
    """Render org tags.

    ``:a: :b:`` reads as a list of tags, which suits a labelled line;
    ``:a:b:`` is how org writes them in a file, which suits echoing back
    what was captured.
    """
    if not tags:
        return ""
    if separate:
        return " ".join(f":{tag}:" for tag in tags)
    return "".join(f":{tag}" for tag in tags) + ":"


def format_datetime(entry: Entry) -> str:
    """A git-log style date line value."""
    if entry.timestamp is not None:
        return entry.timestamp.strftime("%a %b %-d %H:%M:%S %Y")
    return entry.when.strftime("%a %b %-d %Y")


def format_entry(entry: Entry, colour: bool = False) -> str:
    """Render one entry in the shape of a ``git log`` commit."""
    lines = [
        style(f"entry {entry.short_ident()}", "yellow", colour)
        + style(f"  ({entry.path.name})", "cyan", colour)
    ]
    lines.append(f"Date:   {format_datetime(entry)}")
    if entry.tags:
        lines.append("Tags:   " + format_tags(entry.tags))
    lines.append("")

    heading = entry.heading or "(untitled)"
    if entry.todo:
        heading = f"{entry.todo} {heading}"
    lines.append("    " + style(heading, None, colour, bold=True))

    body = entry.body.strip("\n")
    if body:
        lines.append("")
        lines.append(indent(body))
    return "\n".join(lines)


def format_oneline(entry: Entry, colour: bool = False) -> str:
    stamp = (
        entry.timestamp.strftime("%Y-%m-%d %H:%M")
        if entry.timestamp is not None
        else entry.day.isoformat() + "      "
    )
    heading = entry.heading or "(untitled)"
    if entry.todo:
        heading = f"{entry.todo} {heading}"
    return (
        style(entry.short_ident(), "yellow", colour)
        + f" {stamp} "
        + heading
    )


def format_log(
    entries: Sequence[Entry], oneline: bool = False, colour: bool = False
) -> str:
    if oneline:
        return "\n".join(format_oneline(e, colour) for e in entries)
    return "\n\n".join(format_entry(e, colour) for e in entries)


def tildify(path: Path) -> str:
    """Abbreviate a path under the home directory the way a shell does."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except (ValueError, RuntimeError):
        return str(path)


def format_capture_summary(
    where: str, title: str, path: Path | None = None, tags: Sequence[str] = ()
) -> str:
    """The confirmation ``commit`` and ``note`` print after capturing."""
    lines = [f"[{where}] {title}"]
    if path is not None:
        lines.append(f" 1 entry added to {tildify(path)}")
    if tags:
        lines.append(" tags: " + format_tags(tags, separate=False))
    return "\n".join(lines)


def format_templates(templates: Iterable) -> str:
    """Tabulate templates for ``commit --list-templates``."""
    rows = list(templates)
    if not rows:
        return "no dailies capture templates are defined"
    width = max(len(t.key) for t in rows)
    lines = []
    for index, template in enumerate(rows):
        lines.append(f"  [{index}] {template.key:<{width}}  {template.description}")
    return "\n".join(lines)


def format_store_list(summaries: Sequence, colour: bool = False) -> str:
    """One line per stored document, id first."""
    if not summaries:
        return "no documents in the store"
    width = max(len(s.doc_id) for s in summaries)
    lines = []
    for summary in summaries:
        line = style(f"{summary.doc_id:<{width}}", "yellow", colour)
        if summary.error:
            lines.append(line + "  " + style(f"({summary.error})", "red", colour))
            continue
        line += f"  {summary.title or '(untitled)'}"
        if summary.filetags:
            tags = format_tags(summary.filetags, separate=False)
            line += "  " + style(tags, "cyan", colour)
        lines.append(line)
    return "\n".join(lines)


def format_store_summary(summary, colour: bool = False) -> str:
    """The detail ``store show`` prints for one document."""
    lines = [
        style(f"document {summary.doc_id}", "yellow", colour),
        f"Title:  {summary.title or '(untitled)'}",
        f"Hash:   {summary.content_hash[:16]}",
        f"Schema: {summary.schema_version}",
    ]
    if summary.filetags:
        lines.append("File tags: " + format_tags(summary.filetags))
    if summary.tags:
        lines.append("Tags:   " + format_tags(summary.tags))
    lines.append("")
    lines.append(
        f"    {summary.nodes} nodes, {summary.headings} headings, "
        f"{summary.links} links, {summary.timestamps} timestamps"
    )
    return "\n".join(lines)


def format_node(summary, colour: bool = False) -> str:
    """A single node, identified and summarised."""
    head = style(f"node {summary.ref}", "yellow", colour)
    head += style(f"  ({summary.type})", "cyan", colour)
    if not summary.text:
        return head
    return head + "\n\n" + indent(summary.text)


def format_search_results(results: Sequence, colour: bool = False) -> str:
    """One block per hit: where it is, then the matching text."""
    lines = []
    for result in results:
        head = style(f"{result.doc_id}:{result.ref}", "yellow", colour)
        head += style(f"  ({result.type})", "cyan", colour)
        lines.append(head)
        excerpt = " ".join(result.excerpt.split())
        if excerpt:
            lines.append(indent(excerpt))
    return "\n".join(lines)


def format_sync_report(report, heading: str, colour: bool = False) -> str:
    """What a sync did, one line per document that changed."""
    lines = [style(heading, "yellow", colour)]
    if report.dry_run:
        lines[0] += style("  (dry run, nothing written)", "cyan", colour)

    marks = {"created": "+", "updated": "~", "deleted": "-", "failed": "!"}
    for change in report.changes:
        outcome = change.outcome.value
        if outcome == "unchanged":
            continue
        line = f"  {marks.get(outcome, ' ')} {change.doc_id}"
        if change.detail:
            line += f"  ({change.detail})"
        lines.append(line)

    counts: dict[str, int] = {}
    for change in report.changes:
        counts[change.outcome.value] = counts.get(change.outcome.value, 0) + 1
    summary = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    lines.append("")
    lines.append(f"  {summary or 'nothing to do'}")
    return "\n".join(lines)


def resolve_colour(mode: str, stream=None) -> bool:
    """Interpret --color=auto|always|never."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def pager_argv(configured: str | None) -> list[str] | None:
    """Choose a pager command, or None to write straight to stdout."""
    candidate = configured or os.environ.get("PAGER")
    if candidate is not None:
        candidate = candidate.strip()
        if candidate in {"", "cat"}:
            return None
        return ["/bin/sh", "-c", f'{candidate} "$@"', candidate]
    for fallback in ("less", "more"):
        found = shutil.which(fallback)
        if found:
            return [found]
    return None


def write(text: str, colour: bool = False) -> None:
    """Write TEXT to stdout, keeping ANSI when colour was asked for.

    `click.echo` strips styling whenever stdout is not a terminal, which
    would silently defeat ``--color=always`` in a pipe.
    """
    click.echo(text, nl=False, color=colour or None)


def page(
    text: str, configured: str | None, force: bool = False, colour: bool = False
) -> None:
    """Send TEXT through a pager when attached to a terminal."""
    if not text:
        return
    if not text.endswith("\n"):
        text += "\n"

    if not force and not sys.stdout.isatty():
        write(text, colour)
        return

    argv = pager_argv(configured)
    if argv is None:
        write(text, colour)
        return

    env = dict(os.environ)
    env.setdefault("LESS", LESS_DEFAULTS)
    env.setdefault("LV", "-c")
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, env=env, text=True)
    except OSError:
        write(text, colour)
        return
    try:
        process.communicate(text)
    except (BrokenPipeError, KeyboardInterrupt):
        pass

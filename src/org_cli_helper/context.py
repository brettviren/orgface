# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The context a command was run in, as a capture template.

``org note`` records not only what was written but where and when: the
working directory, the host, the user, and the time to the second.  Those
facts are gathered here and turned into an ordinary capture template body,
so a note is selected, filled and escaped by :mod:`org_cli_helper.templates`
by exactly the same route as a commit.

Nothing here reads a clock or an environment on its own behalf --
:func:`gather` takes every fact as an argument and only falls back to the
running process for the ones it was not given, which is what makes the
result reproducible in a test.
"""

from __future__ import annotations

import getpass
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import templates as tmpl

# Characters org keeps out of a headline tag.  `org-tag-re' also admits %,
# but a % in a template body is org-capture's escape character and the
# backslash that would protect it is itself not a tag character, so a tag
# is the one place we cannot have one.
_NOT_TAG = re.compile(r"[^A-Za-z0-9_@#]+")

#: Tag added when the note was written from the user's home directory.
HOME_TAG = "home"

#: What marks the root of a git working tree.
GIT_MARKER = ".git"

#: Where the writer's own text goes; org-capture's cursor marker.
BODY_MARKER = "%?"

#: Prompt org-capture shows for the title, and where a ``-m`` title lands.
TITLE_PROMPT = "%^{Title}"


def org_tag(text: str) -> str:
    """Reduce TEXT to something org will accept as a headline tag.

    Org allows only alphanumerics and ``_@#`` in a tag, so a directory
    named ``org-cli-helper`` cannot be one as it stands; runs of anything
    else collapse to a single underscore.  Returns ``""`` when nothing
    usable is left.
    """
    return _NOT_TAG.sub("_", text).strip("_")


def git_root(start: Path) -> Path | None:
    """The nearest directory at or above START holding a ``.git``.

    A ``.git`` file rather than a directory is what git leaves in a
    worktree or a submodule checkout, and that is still the root of a
    working tree, so both count.
    """
    for candidate in (start, *start.parents):
        if (candidate / GIT_MARKER).exists():
            return candidate
    return None


def hostname() -> str:
    return socket.gethostname()


def username() -> str:
    """The login name, or ``"unknown"`` where there is no telling."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def home() -> Path | None:
    """The user's home directory, or None if it cannot be determined."""
    try:
        return Path.home()
    except RuntimeError:
        return None


@dataclass(frozen=True)
class RunContext:
    """Where, when and by whom a command was run."""

    directory: Path
    when: datetime
    host: str
    user: str
    git_root: Path | None = None
    home: Path | None = None

    @property
    def tags(self) -> tuple[str, ...]:
        """Headline tags describing the directory."""
        found: list[str] = []
        if self.home is not None and self.directory == self.home:
            found.append(HOME_TAG)
        if self.git_root is not None:
            tag = org_tag(self.git_root.name)
            if tag and tag not in found:
                found.append(tag)
        return tuple(found)

    @property
    def timestamp(self) -> str:
        """The run time as an inactive org timestamp, to the second.

        Inactive so that a note recording when something was written does
        not also turn up in the agenda as something to be done.
        """
        return self.when.strftime("[%Y-%m-%d %a %H:%M:%S]")

    def properties(self) -> tuple[tuple[str, str], ...]:
        """The context as org property drawer entries."""
        return (
            ("DIRECTORY", str(self.directory)),
            ("HOST", self.host),
            ("USER", self.user),
        )


def gather(
    directory: Path | None = None,
    now: datetime | None = None,
    host: str | None = None,
    user: str | None = None,
    where_home: Path | None = None,
) -> RunContext:
    """Look up the context the command is running in.

    Paths are resolved so that the comparison against the home directory
    is not defeated by a symlink on either side.
    """
    directory = (directory if directory is not None else Path.cwd()).resolve()
    where_home = where_home if where_home is not None else home()
    return RunContext(
        directory=directory,
        # Org timestamps stop at the minute in their usual form and we
        # promise seconds, but never more: microseconds are noise here.
        when=(now if now is not None else datetime.now()).replace(microsecond=0),
        host=host if host is not None else hostname(),
        user=user if user is not None else username(),
        git_root=git_root(directory),
        home=where_home.resolve() if where_home is not None else None,
    )


def note_template(
    context: RunContext, key: str = "n", description: str = "context note"
) -> tmpl.Template:
    """Build the capture template that records CONTEXT.

    The result is shaped like any other capture template, which is the
    point: :func:`~org_cli_helper.templates.fill` puts a ``-m`` title in
    the ``%^{Title}`` prompt and the body at ``%?``, escaping both, and
    left unfilled that same prompt asks Emacs' user for the title.

    The context values are spliced in here rather than filled, so they are
    escaped here too.
    """
    tags = context.tags
    suffix = " :" + ":".join(tags) + ":" if tags else ""

    lines = [f"* {TITLE_PROMPT}{suffix}", ":PROPERTIES:"]
    for name, value in context.properties():
        tmpl.check_expandable(value, f"{name.lower()} of the note")
        lines.append(f":{name}: {tmpl.escape(value)}")
    lines += [":END:", context.timestamp, "", BODY_MARKER]

    return tmpl.Template(
        key=key, description=description, type="entry", body="\n".join(lines)
    )

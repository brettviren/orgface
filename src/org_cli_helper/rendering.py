# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Which documents ``org render`` renders, and where the text goes.

Working out a destination is a decision, not argument parsing, so it
lives here rather than in the command: the rules about one document
versus many, about ``-`` meaning standard output, and about what is a
file and what is a directory, are all testable without a CLI runner.

Every destination is settled *before* anything is written.  Rendering a
hundred documents and discovering at the ninety-ninth that the output
directory is really a file would leave a job half done, and half a job
is the hardest kind to undo.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Where several renderings go when nothing says otherwise.
DEFAULT_DIRECTORY = Path("output")

#: What ``--output`` is when it means standard output.
STDOUT = "-"


class RenderError(Exception):
    """A rendering was asked for that cannot be carried out as asked."""


@dataclass(frozen=True)
class Destination:
    """One document, and the file its rendering belongs in.

    A ``path`` of None means standard output.
    """

    doc_id: str
    path: Path | None

    @property
    def to_stdout(self) -> bool:
        return self.path is None


def resolve(names: Iterable[str], known: Sequence[str]) -> list[str]:
    """The document ids NAMES refer to, in the order given.

    A name that is not an id but names an org file that is -- ``notes``
    for ``notes.org`` -- resolves to it, because a person naming files on
    a command line should not have to remember which form the store used.
    """
    have = set(known)
    found: list[str] = []
    for name in names:
        if name in have:
            found.append(name)
        elif f"{name}.org" in have:
            found.append(f"{name}.org")
        else:
            raise RenderError(f"no such document in the store: {name}")
    return found


def destinations(
    doc_ids: Sequence[str],
    output: str | None,
    extension: str,
    *,
    everything: bool = False,
) -> list[Destination]:
    """Where each of DOC_IDS should be written.

    With one document, OUTPUT names the file -- or a directory to put it
    in, if that is what it already is -- and ``-`` means standard output.
    With several, OUTPUT names a directory, and standard output is not an
    answer: a hundred documents down one pipe is a hundred documents lost.

    ``everything`` is what ``--all`` asked for, and it means a directory
    however many documents happen to be in the store today.  Where a
    rendering lands should follow from what was asked for, not from how
    much of it there turned out to be.
    """
    if not doc_ids:
        return []
    if len(doc_ids) == 1 and not everything:
        return [_single(doc_ids[0], output, extension)]
    return _several(doc_ids, output, extension)


def write(destination: Destination, text: str) -> Path:
    """Write TEXT where DESTINATION says, making the directory it needs."""
    if destination.path is None:  # pragma: no cover - the caller checks first
        raise RenderError("this destination is standard output, not a file")
    destination.path.parent.mkdir(parents=True, exist_ok=True)
    destination.path.write_text(text, encoding="utf-8")
    return destination.path


# -- internals --------------------------------------------------------------


def _single(doc_id: str, output: str | None, extension: str) -> Destination:
    if output == STDOUT:
        return Destination(doc_id, None)
    # One document with nowhere named goes to the current directory, so
    # only its name is kept: the store's own layout is not something to
    # recreate here when a person asked for one file.
    name = _filename(Path(doc_id).name, extension)
    if output is None:
        return Destination(doc_id, Path(name))
    chosen = Path(output).expanduser()
    if chosen.is_dir():
        return Destination(doc_id, chosen / name)
    return Destination(doc_id, chosen)


def _several(
    doc_ids: Sequence[str], output: str | None, extension: str
) -> list[Destination]:
    if output == STDOUT:
        raise RenderError(
            "standard output takes one named document at a time, not "
            f"{len(doc_ids)}; give a directory instead"
        )
    directory = DEFAULT_DIRECTORY if output is None else Path(output).expanduser()
    if directory.exists() and not directory.is_dir():
        raise RenderError(
            f"{directory} is a file, and rendering {len(doc_ids)} documents "
            "needs a directory to put them in"
        )
    return [
        Destination(doc_id, _within(directory, doc_id, extension))
        for doc_id in doc_ids
    ]


def _within(directory: Path, doc_id: str, extension: str) -> Path:
    """Where one document goes inside DIRECTORY.

    A document id that is a path keeps its shape, so a store laid out in
    subdirectories renders into the same subdirectories rather than
    collapsing into one heap of colliding names.  An id that would climb
    out of the directory is refused: the store may hold anything, and
    ``--output`` named one place to write.
    """
    relative = Path(_filename(doc_id, extension))
    target = (directory / relative).resolve()
    if directory.resolve() not in target.parents:
        raise RenderError(f"{doc_id} would be written outside {directory}")
    return directory / relative


def _filename(doc_id: str, extension: str) -> str:
    """The file a document's rendering is called."""
    return str(Path(doc_id).with_suffix(extension))

"""Reading org-roam daily notes into log entries.

Daily notes are plain files, so ``org log`` reads them directly rather than
paying for a round trip to Emacs.  Only the locations of the notes come from
the daemon, and those are cached.

Each top level heading in a daily note is one entry -- the unit an
``org commit`` creates -- which makes it the natural analogue of a commit.
Any content ahead of the first heading becomes a preamble entry so nothing
in a hand-edited file is silently dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

# Daily note file names, per the stock "%<%Y-%m-%d>.org" target.
DAILY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.org$")

_PROPERTY_DRAWER_START = re.compile(r"^\s*:PROPERTIES:\s*$", re.IGNORECASE)
_PROPERTY_DRAWER_END = re.compile(r"^\s*:END:\s*$", re.IGNORECASE)
_PROPERTY_RE = re.compile(r"^\s*:([^:]+):\s*(.*?)\s*$")
_KEYWORD_RE = re.compile(r"^\s*#\+(\w[\w-]*):\s*(.*?)\s*$")
_HEADING_RE = re.compile(r"^(\*+)\s+(.*)$")
_TAGS_RE = re.compile(r"\s+(:(?:[\w@#%]+:)+)\s*$")
_TODO_RE = re.compile(r"^([A-Z][A-Z0-9_-]{1,})\s+(.*)$")

# Active <...> and inactive [...] org timestamps.
_TIMESTAMP = (
    r"[<\[](\d{4})-(\d{2})-(\d{2})(?:\s+[^\s\]>]+)?"
    r"(?:\s+(\d{1,2}):(\d{2}))?[^\]>]*[>\]]"
)
_TIMESTAMP_RE = re.compile(_TIMESTAMP)

# A line holding nothing but a timestamp, as the stock dailies template's
# "%T" produces.  It is the entry's date rather than part of what was
# written, so it is reported separately instead of twice.
_TIMESTAMP_LINE_RE = re.compile(r"^\s*" + _TIMESTAMP + r"\s*$")


class DailiesError(Exception):
    """The daily notes directory is unusable."""


@dataclass(frozen=True)
class Entry:
    """One heading in a daily note."""

    day: date
    path: Path
    index: int
    heading: str
    body: str
    timestamp: datetime | None = None
    node_id: str | None = None
    todo: str | None = None
    tags: tuple[str, ...] = ()
    level: int = 1

    @property
    def when(self) -> datetime:
        """Best known time for ordering and filtering."""
        if self.timestamp is not None:
            return self.timestamp
        return datetime(self.day.year, self.day.month, self.day.day)

    @property
    def ident(self) -> str:
        """A stable-ish handle, in the spirit of a commit name."""
        base = self.node_id or self.path.stem
        return f"{base}#{self.index}"

    def short_ident(self) -> str:
        base = self.node_id or self.path.stem
        if self.node_id:
            base = self.node_id.split("-")[0]
        return f"{base}#{self.index}"


@dataclass
class DailyNote:
    """A parsed daily note file."""

    day: date
    path: Path
    node_id: str | None = None
    title: str | None = None
    entries: list[Entry] = field(default_factory=list)


def daily_files(directory: Path) -> list[tuple[date, Path]]:
    """Return ``(day, path)`` for every daily note, oldest first."""
    if not directory.is_dir():
        raise DailiesError(f"{directory}: no such directory")
    found: list[tuple[date, Path]] = []
    for path in directory.iterdir():
        match = DAILY_RE.match(path.name)
        if not match or not path.is_file():
            continue
        try:
            day = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            continue  # e.g. 2026-02-31.org
        found.append((day, path))
    found.sort()
    return found


def _parse_timestamp(text: str, fallback: date) -> datetime | None:
    match = _TIMESTAMP_RE.search(text)
    if not match:
        return None
    year, month, day, hour, minute = match.groups()
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour) if hour else 0,
            int(minute) if minute else 0,
        )
    except ValueError:
        return None


def _split_heading(text: str) -> tuple[str | None, str, tuple[str, ...]]:
    """Split a heading into TODO keyword, title, and tags."""
    tags: tuple[str, ...] = ()
    tag_match = _TAGS_RE.search(text)
    if tag_match:
        tags = tuple(t for t in tag_match.group(1).split(":") if t)
        text = text[: tag_match.start()].rstrip()

    todo = None
    todo_match = _TODO_RE.match(text)
    if todo_match and todo_match.group(1) not in {"A", "I"}:
        todo, text = todo_match.group(1), todo_match.group(2)
    return todo, text.strip(), tags


def _strip_drawer(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Pull a leading :PROPERTIES: drawer off LINES."""
    properties: dict[str, str] = {}
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and _PROPERTY_DRAWER_START.match(lines[index]):
        index += 1
        while index < len(lines) and not _PROPERTY_DRAWER_END.match(lines[index]):
            match = _PROPERTY_RE.match(lines[index])
            if match:
                properties[match.group(1).upper()] = match.group(2)
            index += 1
        index += 1  # consume :END:
        return properties, lines[index:]
    return properties, lines


def parse_note(path: Path, day: date | None = None) -> DailyNote:
    """Parse one daily note into its entries."""
    if day is None:
        match = DAILY_RE.match(path.name)
        if not match:
            raise DailiesError(f"{path.name}: not a daily note file name")
        day = date(int(match[1]), int(match[2]), int(match[3]))

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    properties, lines = _strip_drawer(lines)
    note = DailyNote(day=day, path=path, node_id=properties.get("ID"))

    preamble: list[str] = []
    first_heading = len(lines)
    for position, line in enumerate(lines):
        if _HEADING_RE.match(line):
            first_heading = position
            break
        keyword = _KEYWORD_RE.match(line)
        if keyword:
            if keyword.group(1).lower() == "title":
                note.title = keyword.group(2)
            continue
        preamble.append(line)

    counter = 0
    if "".join(preamble).strip():
        body = "\n".join(preamble).strip("\n")
        note.entries.append(
            Entry(
                day=day,
                path=path,
                index=counter,
                heading=note.title or path.stem,
                body=body,
                timestamp=_parse_timestamp(body, day),
                node_id=note.node_id,
                level=0,
            )
        )
        counter += 1

    current: list[str] | None = None
    heading_text = ""
    level = 1
    for line in lines[first_heading:]:
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            if current is not None:
                note.entries.append(
                    _make_entry(note, counter, heading_text, current, level)
                )
                counter += 1
            heading_text = match.group(2)
            level = 1
            current = []
        elif current is not None:
            current.append(line)
    if current is not None:
        note.entries.append(_make_entry(note, counter, heading_text, current, level))

    return note


def _promote_timestamp_line(lines: list[str]) -> tuple[datetime | None, list[str]]:
    """Lift a leading timestamp-only line out of LINES.

    The stock dailies template writes the capture time on its own line, so
    that line is metadata rather than prose.  A timestamp appearing amongst
    other text is left where the writer put it.
    """
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and _TIMESTAMP_LINE_RE.match(lines[index]):
        stamp = _parse_timestamp(lines[index], date.today())
        if stamp is not None:
            return stamp, lines[index + 1 :]
    return None, lines


def _make_entry(
    note: DailyNote, index: int, heading: str, body_lines: list[str], level: int
) -> Entry:
    todo, title, tags = _split_heading(heading)
    drawer, remaining = _strip_drawer(body_lines)
    timestamp, remaining = _promote_timestamp_line(remaining)
    body = "\n".join(remaining).strip("\n")
    if timestamp is None:
        timestamp = _parse_timestamp("\n".join(remaining), note.day)
    return Entry(
        day=note.day,
        path=note.path,
        index=index,
        heading=title,
        body=body,
        timestamp=timestamp,
        node_id=drawer.get("ID") or note.node_id,
        todo=todo,
        tags=tags,
        level=level,
    )


def iter_entries(
    directory: Path,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[Entry]:
    """Yield entries within the bounds, newest first."""
    files = daily_files(directory)

    # Filter whole files by day before reading them.
    for day, path in reversed(files):
        if since is not None and day < since.date():
            continue
        if until is not None and day > until.date():
            continue
        note = parse_note(path, day)
        for entry in sorted(note.entries, key=lambda e: (e.when, e.index), reverse=True):
            when = entry.when
            if since is not None and when < since:
                continue
            if until is not None and when > until:
                continue
            yield entry


def collect(
    directory: Path,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    reverse: bool = False,
) -> list[Entry]:
    """Gather entries newest first, honouring bounds and a count limit."""
    entries: list[Entry] = []
    for entry in iter_entries(directory, since=since, until=until):
        entries.append(entry)
        if limit is not None and not reverse and len(entries) >= limit:
            break
    if limit is not None and reverse:
        entries = entries[:limit]
    if reverse:
        entries.reverse()
    return entries


def resolve_directory(roam: Path, dailies: str) -> Path:
    """Combine org-roam-directory with org-roam-dailies-directory."""
    return (roam / dailies).resolve()


def summary(entries: Sequence[Entry]) -> dict[str, int]:
    """Counts used by the log footer."""
    return {
        "entries": len(entries),
        "days": len({entry.day for entry in entries}),
    }

# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""GitHub: getting its data into files, and making org of it.

Two kinds of file are kept, because GitHub offers two kinds of data:

``recent``
    What the API says, fetched by ghexport.  The API remembers only the
    last few weeks of events, so each fetch is a dated file and the older
    ones are kept.

``archive``
    The account export GitHub emails on request (Settings, Account,
    Export account data).  It holds everything, but most of its bulk is
    the git repositories, which are of no use here, so only its metadata
    -- the JSON files at its top level -- is kept.

HPI, which the org side reads through, takes both as directories of files.
The ``HPI config`` it uses must point at the directories used here.

Nothing in this module needs ghexport, orger or HPI until it is asked to
use them, so it can be imported to find out whether they are missing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tarfile
import tempfile
from argparse import Namespace
from datetime import date
from pathlib import Path
from typing import Any

from ... import passstore, xdgpaths
from ...config import parse_bool, parse_path, parse_str
from .. import setting

log = logging.getLogger(__name__)


class GithubError(Exception):
    """Something the user can act on went wrong."""


SLURP_SETTINGS = (
    setting(
        "slurp", "github", "token", None, parse_str,
        "GitHub API token; prefer token_passname or the environment variable.",
        secret=True,
    ),
    setting(
        "slurp", "github", "token_passname", None, parse_str,
        "pass(1) entry whose first line is the API token, used when token is unset.",
    ),
    setting(
        "slurp", "github", "recent_directory", None, parse_path,
        "Where the dated API dumps go; defaults under the XDG data directory.",
    ),
    setting(
        "slurp", "github", "archive_directory", None, parse_path,
        "Where the account export's JSON files go; defaults under the XDG data directory.",
    ),
    setting(
        "slurp", "github", "include_traffic", False, parse_bool,
        "Also fetch repository traffic; slow, and only allowed on repositories you can push to.",
    ),
)

#: The styles orger can give timestamps.
TIMESTAMPS = ("inactive", "active", "plain", "none")

ORGER_SETTINGS = (
    setting(
        "orger", "github", "output", None, parse_path,
        "The org file to write; 'github.org' in the current directory when unset.",
    ),
    setting(
        "orger", "github", "pandoc", True, parse_bool,
        "Convert bodies to org with pandoc; nicer, but slow for a lot of text.",
    ),
    setting(
        "orger", "github", "timestamps", "inactive", parse_str,
        f"Timestamp style, one of: {', '.join(TIMESTAMPS)}.",
    ),
    setting(
        "orger", "github", "hpi_config", None, parse_path,
        "HPI's config directory, the one holding my/config; $MY_CONFIG or ~/.config/my when unset.",
    ),
)


# -- slurp ------------------------------------------------------------------


def _default_directory(configured: Path | None, *names: str) -> Path:
    return configured or xdgpaths.data_dir().joinpath(*names)


def recent_directory(settings: dict[str, Any]) -> Path:
    return _default_directory(settings["recent_directory"], "slurp", "github", "recent")


def archive_directory(settings: dict[str, Any]) -> Path:
    return _default_directory(settings["archive_directory"], "slurp", "github", "archive")


def token(settings: dict[str, Any]) -> str:
    """The API token, from wherever it was configured."""
    try:
        found = passstore.secret(settings["token"], settings["token_passname"])
    except passstore.PassError as exc:
        raise GithubError(f"GitHub token: {exc}") from exc
    if not found:
        raise GithubError(
            "no GitHub token: give --token, set ORGFACE_SLURP_GITHUB_TOKEN, "
            "or set token or token_passname under [slurp.github] in the config"
        )
    return found


def export_recent(api_token: str, include: tuple[str, ...], traffic: bool) -> Any:
    """Ask GitHub's API for the account's data, as ghexport does."""
    import github
    from ghexport import export

    fields = getattr(export, "_ALL_FIELDS", None)
    unknown = [name for name in include if fields is not None and name not in fields]
    if unknown:
        raise GithubError(
            f"cannot include {', '.join(unknown)}; the choices are {', '.join(fields)}"
        )
    log.info("fetching from the GitHub API")
    try:
        return export.get_json(
            token=api_token,
            include=list(include) or None,
            include_repos_traffic=traffic,
        )
    except github.GithubException as exc:
        raise GithubError(f"GitHub API: {exc}") from exc


def write_dump(data: Any, directory: Path, today: date | None = None) -> Path:
    """Keep ``data`` as the dump for ``today``, replacing one from earlier today.

    The file appears all at once, so a reader never meets a half-written
    dump.  The temporary file is made beside the directory, not in it,
    because HPI reads every file in the directory.
    """
    today = today or date.today()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"ghexport-{today.isoformat()}.json"
    handle, temporary = tempfile.mkstemp(prefix=f".{directory.name}.", dir=directory.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
        # mkstemp makes a private file; a dump should be like any other.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(temporary, 0o666 & ~umask)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


_TOP_LEVEL_JSON = re.compile(r"^(?:\./)?([^/]+\.json)$")


def unpack_archive(archive: Path, directory: Path) -> list[str]:
    """Keep the JSON files at the top level of a GitHub account export.

    Everything else in the archive, which is nearly all of it, is skipped
    without being written, and names are matched, never used as paths, so
    nothing can land outside ``directory``.  The files already in
    ``directory`` are replaced by the new ones only once all of those
    have been read, so an archive that fails part way changes nothing.
    ``schema.json`` has to be among them: it is how HPI recognises the
    directory as an unpacked export.

    Returns the names of the files kept.
    """
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}.", dir=directory.parent))
    try:
        names: list[str] = []
        try:
            # Streaming: a gzip tar cannot be searched, only read through.
            with tarfile.open(archive, "r|*") as tar:
                for member in tar:
                    found = _TOP_LEVEL_JSON.match(member.name)
                    if not found or not member.isfile():
                        continue
                    stream = tar.extractfile(member)
                    assert stream is not None  # a regular file
                    (staging / found.group(1)).write_bytes(stream.read())
                    names.append(found.group(1))
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise GithubError(f"{archive}: {exc}") from exc
        if "schema.json" not in names:
            raise GithubError(
                f"{archive}: no schema.json at the top level; "
                "is it a GitHub account export?"
            )
        directory.mkdir(exist_ok=True)
        for old in directory.glob("*.json"):
            old.unlink()
        for name in names:
            os.replace(staging / name, directory / name)
        return sorted(names)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# -- orger ------------------------------------------------------------------


def hpi_config_directory(configured: Path | None) -> Path | None:
    """The directory holding HPI's ``my/config``, if there is one.

    HPI looks for it in ``$MY_CONFIG``, else ``~/.config/my``.  Modules of
    the ``my`` package found there, such as a replacement for
    ``my.github.all``, take precedence over HPI's own only when that
    directory is searched first, which HPI itself does too late.
    """
    if configured is not None:
        directory = configured
    elif os.environ.get("MY_CONFIG"):
        directory = Path(os.environ["MY_CONFIG"]).expanduser()
    else:
        directory = xdgpaths.config_home() / "my"
    return directory if (directory / "my").is_dir() else None


def render(
    *,
    to: Path | None,
    stdout: bool,
    pandoc: bool,
    timestamps: str,
    hpi_config: Path | None,
) -> Path | None:
    """Write the org file, as ``python -m orger.modules.github`` does.

    Returns the file written, or None if it went to the standard output.
    """
    if timestamps not in TIMESTAMPS:
        raise GithubError(f"timestamps: {timestamps!r} is not one of {', '.join(TIMESTAMPS)}")
    directory = hpi_config_directory(hpi_config)
    if directory is not None and str(directory) not in sys.path:
        log.debug("searching %s for HPI modules first", directory)
        sys.path.insert(0, str(directory))

    from orger.modules.github import Github

    view = Github(cmdline_args=Namespace(pandoc=pandoc, timestamps=timestamps))
    view.main_common()
    target = to or Path("github.org")
    view._run(to=target, stdout=stdout)
    return None if stdout else target

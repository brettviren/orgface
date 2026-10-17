# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reading secrets from the standard Unix password manager, pass(1).

A secret kept in pass need never be written into a config file or an
environment variable: the config names the entry, and the value is
fetched only when something actually needs it.
"""

from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger(__name__)


class PassError(Exception):
    """The pass entry could not be read."""


def lookup(passname: str, program: str = "pass") -> str:
    """Return the first line of ``pass <passname>``.

    By convention the first line of a pass entry is the secret itself and
    any further lines are notes about it.
    """
    log.debug(
        "running %s %s; gpg may be waiting for a passphrase if this stalls",
        program, passname,
    )
    started = time.monotonic()
    try:
        result = subprocess.run(
            [program, passname], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise PassError(f"cannot run {program}: {exc}") from exc
    log.debug(
        "%s %s exited %d after %.2fs",
        program, passname, result.returncode, time.monotonic() - started,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise PassError(f"{program} {passname}: {detail}")
    secret = result.stdout.partition("\n")[0].strip()
    if not secret:
        raise PassError(f"{program} {passname}: first line is empty")
    return secret
